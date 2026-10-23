"""Direct PG migration: convert old short-hex enzyme IDs to valid UUIDs.

One-off maintenance script. Connection comes from CE_PG_* env vars (see .env).
"""
import asyncio
import os
import sys
import uuid

import asyncpg
import dotenv

# Ensure we can import app modules regardless of invocation directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config

dotenv.load_dotenv()
PG_DSN = Config.from_env().pg.dsn


def hex_to_uuid(short_hex: str) -> str:
    """Pad a 12-char hex to a deterministic UUID."""
    padded = short_hex.ljust(32, "0")[:32]
    return str(uuid.UUID(padded))


async def migrate():
    conn = await asyncpg.connect(PG_DSN)
    print("✅ Connected to PG")

    # Find all non-UUID IDs
    rows = await conn.fetch("SELECT id FROM enzymes WHERE length(id) != 36")
    print(f"📋 Found {len(rows)} enzymes with non-UUID IDs")

    if not rows:
        print("🎉 All IDs are already valid UUIDs!")
        await conn.close()
        return

    for row in rows:
        old_id = row["id"]
        new_id = hex_to_uuid(old_id)
        print(f"  🔄 {old_id} → {new_id}")

        # Update in a transaction (FKs cascade via the UPDATE)
        async with conn.transaction():
            # Temporarily drop FK constraints, update, re-add
            # Simpler: just update the enzymes table + related tables
            await conn.execute(
                "UPDATE evolution_log SET enzyme_id = $1 WHERE enzyme_id = $2",
                new_id, old_id,
            )
            await conn.execute(
                "UPDATE enzyme_relations SET source_id = $1 WHERE source_id = $2",
                new_id, old_id,
            )
            await conn.execute(
                "UPDATE enzyme_relations SET target_id = $1 WHERE target_id = $2",
                new_id, old_id,
            )
            await conn.execute(
                "UPDATE enzymes SET id = $1 WHERE id = $2",
                new_id, old_id,
            )

    # Verify
    bad = await conn.fetchval("SELECT count(*) FROM enzymes WHERE length(id) != 36")
    total = await conn.fetchval("SELECT count(*) FROM enzymes")
    print(f"\n📊 Total: {total} | Still invalid: {bad}")
    if bad == 0:
        print("🎉 所有 enzyme ID 已迁移为合法 UUID！")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
