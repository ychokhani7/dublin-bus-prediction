import os
import httpx
import asyncpg
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google.transit import gtfs_realtime_pb2
from contextlib import asynccontextmanager

# Pull secrets securely from the environment
NTA_API_KEY = os.getenv("NTA_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

NTA_URL = "https://api.nationaltransport.ie/gtfsr/v2/Vehicles"

scheduler = AsyncIOScheduler()
db_pool = None

async def fetch_realtime_data():
    headers = {"Cache-Control": "no-cache", "x-api-key": NTA_API_KEY}
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(NTA_URL, headers=headers, timeout=10.0)
            response.raise_for_status()
            
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(response.content)
            
            # 1. Extract all bus data into a single Python list
            records = []
            for entity in feed.entity:
                if entity.HasField('vehicle'):
                    v = entity.vehicle
                    if v.position.latitude and v.position.longitude:
                        # Append a tuple of the data for this specific bus
                        records.append((
                            v.trip.route_id, 
                            v.trip.trip_id, 
                            v.position.latitude, 
                            v.position.longitude
                        ))
            
            # 2. Perform a single bulk insert to the database
            if records:
                async with db_pool.acquire() as conn:
                    await conn.executemany("""
                        INSERT INTO vehicle_positions 
                        (route_id, trip_id, latitude, longitude, location) 
                        VALUES ($1, $2, $3, $4, ST_SetSRID(ST_MakePoint($4, $3), 4326))
                    """, records)
                        
            print(f"✅ Successfully bulk-logged {len(records)} vehicle positions to Supabase.")
                    
        except Exception as e:
            print(f"❌ Ingestion failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    
    # Initialize the database connection pool on startup
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    # Start the background polling task every 60 seconds
    scheduler.add_job(fetch_realtime_data, 'interval', seconds=60)
    scheduler.start()
    
    yield
    
    # Clean up on shutdown
    scheduler.shutdown()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def health_check():
    return {"status": "Database ingestion engine is running"}
