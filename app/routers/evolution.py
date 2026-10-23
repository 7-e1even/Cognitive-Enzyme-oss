"""Evolution timeline router."""

import json

from fastapi import APIRouter, Depends, Query

from app.core.engine import Engine
from app.dependencies import get_engine
from app.routers._helpers import event_row_to_dict

router = APIRouter(prefix="/api/evolution", tags=["evolution"])


@router.get("")
async def get_evolution(
    enzyme_id: str = "",
    event_type: str = "",
    limit: int = Query(50, ge=1, le=500),
    engine: Engine = Depends(get_engine),
):
    return {"events": await _get_evolution_events(engine, enzyme_id, event_type, limit)}


@router.get("/{enzyme_id}")
async def get_enzyme_evolution(
    enzyme_id: str,
    limit: int = Query(20, ge=1, le=500),
    engine: Engine = Depends(get_engine),
):
    return {"events": await _get_evolution_events(engine, enzyme_id, "", limit)}


# ── Helpers ───────────────────────────────────────────────────

async def _get_evolution_events(
    engine: Engine, enzyme_id: str, event_type: str, limit: int,
) -> list[dict]:
    clauses, params = [], []
    idx = 1
    if enzyme_id:
        clauses.append(f"enzyme_id = ${idx}")
        params.append(enzyme_id)
        idx += 1
    if event_type:
        clauses.append(f"event_type = ${idx}")
        params.append(event_type)
        idx += 1
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM evolution_log {where} ORDER BY created_at DESC LIMIT ${idx}"
    params.append(limit)
    async with engine.pg._pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [event_row_to_dict(r) for r in rows]

