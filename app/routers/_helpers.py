"""Shared helpers for routers."""

from __future__ import annotations

import json
import logging

from app import settings

logger = logging.getLogger(__name__)


def capabilities(config) -> dict[str, bool]:
    """四个模型环节此刻能不能用:引擎 LLM、向量、精排、适用性判官(首页、总览与系统状态共用)。"""
    emb = config.embedding
    return {
        "llm": config.llm.available,
        "embedding": bool(emb.api_key and emb.api_url),
        "rerank": bool(emb.rerank_api_url and (emb.rerank_api_key or emb.api_key)),
        "judge": config.typesafe.available and settings.get("judge.enabled"),
    }


async def audit(engine, user: dict, action: str, target: str = "", detail: dict | None = None) -> None:
    """记一条审计日志(best-effort:写失败只记警告,不影响操作本身)。"""
    actor = user.get("username", "") if isinstance(user, dict) else ""
    try:
        await engine.pg.audit(actor, action, target, detail or {})
    except Exception:
        logger.warning("audit log write failed", exc_info=True)


def event_row_to_dict(row) -> dict:
    """Convert a DB evolution_log row to a serializable dict."""
    d = dict(row)
    detail = d.get("detail", "{}")
    if isinstance(detail, str):
        detail = json.loads(detail)
    return {
        "id": d.get("id"),
        "enzyme_id": d.get("enzyme_id"),
        "event_type": d.get("event_type"),
        "detail": detail,
        "created_at": d["created_at"].isoformat() if d.get("created_at") else None,
    }
