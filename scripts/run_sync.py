import asyncio
import os
import sys

# Ensure we can import app modules regardless of invocation directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager
import dotenv

async def run():
    dotenv.load_dotenv()
    config = Config.from_env()
    pg = PGStore(config.pg)
    await pg.connect()
    
    qd = QdrantStore(config.qdrant, config.embedding)
    await qd.connect()
    await qd.ensure_collection()
    
    print("Starting full database sync (PG -> Qdrant)...")
    sync = SyncManager(pg, qd)
    await sync.full_sync()
    print("Sync completed.")
    
    await pg.close()
    await qd.close()

if __name__ == "__main__":
    asyncio.run(run())
