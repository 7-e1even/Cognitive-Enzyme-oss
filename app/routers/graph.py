"""Dashboard, metrics, graph, relations, and search router."""

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, Request

from app.core.engine import Engine
from app.dependencies import get_engine
from app.distill.records import decision_log_on
from app.routers._helpers import capabilities, event_row_to_dict

router = APIRouter(prefix="/api", tags=["graph"])

HOME_DAYS = 7           # "帮上忙了吗"看最近一周
HOME_SERIES_DAYS = 14   # 首页那张图看最近两周

# 流程页每一步说明里引用的运行参数(只读、不含密钥):页面按实际配置写,改了参数页面跟着变
FLOW_SETTINGS = (
    "recall.lexical", "recall.lexical_limit", "recall.query_expand", "exemplar.recall", "exemplar.learn",
    "recall.domain_route", "recall.domain_route_q", "recall.domain_route_k", "recall.domain_route_limit",
    "recall.domain_route_timeout",
    "exemplar.analogy", "ranking.relevance_floor", "ranking.dosage_budget", "ranking.exploration_rate",
    "judge.enabled", "judge.pool", "judge.timeout", "judge.rescue", "judge.llm", "judge.credit",
    "judge.two_stage", "judge.scan_reuse_fit", "judge.adaptive_overflow", "judge.overflow_pool",
    "judge.scan_pool", "judge.scan_batch", "judge.compare_k", "judge.concurrency",
    "judge.promote_below", "judge.promote_p", "judge.explore_p", "exemplar.analogy_k",
    "selection.choice_min", "selection.primary_p", "selection.choice_override", "selection.supplement_p",
    "selection.none_max", "selection.rescue_p", "selection.rescue_q", "selection.critical_p",
    "selection.analogy_q", "selection.analogy_p", "distill.decision_log",
    "learning.capture_chars", "learning.skip_known", "learning.dedup", "learning.dedup_tau",
    "domain.auto", "domain.assign_q", "domain.max_count", "domain.merge_below",
)


def _timezone(name: str) -> str:
    """浏览器报来的 IANA 时区名;认不出就按 UTC 切日,不让一个坏参数把首页弄挂。"""
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return "UTC"
    return name


@router.get("/home")
async def get_home(request: Request, tz: str = Query("UTC", max_length=64),
                   engine: Engine = Depends(get_engine)):
    """首页只回答三件事:系统能不能工作、最近有没有帮上忙、有什么要人处理——这里给的就是这三件事要的数字。"""
    summary = await engine.pg.home_summary(days=HOME_DAYS, series_days=HOME_SERIES_DAYS, tz=_timezone(tz))
    try:
        qdrant_ok = await engine.qdrant.ping()
    except Exception:
        qdrant_ok = False
    return {
        # PG 不通时上面那句就已经失败了,走到这里它一定是通的
        "storage": {"postgresql": True, "qdrant": qdrant_ok},
        "capabilities": capabilities(request.app.state.config),
        # 判定记录关着时不记注入与反馈,首页的"帮上忙了吗"要说明为什么是空的
        "decision_log": decision_log_on(),
        **summary,
    }


@router.get("/flow")
async def get_flow():
    """流程页用的实际参数:每一步开没开、阈值多少、质量门分数线、定时巩固的间隔与开关。"""
    from app import settings
    from app.learning import quality_gate as qg
    from app.routers.schedule import _schedule_public, schedule

    return {
        "settings": {key: settings.get(key) for key in FLOW_SETTINGS},
        "quality": {"pass": qg.PASS_THRESHOLD, "refine": qg.REFINE_THRESHOLD, "quarantine": qg.QUARANTINE_THRESHOLD},
        "schedule": {**_schedule_public(), "running": schedule.running, "due_in": max(0, round(schedule.due_in()))},
    }


@router.get("/metrics")
async def get_metrics(engine: Engine = Depends(get_engine)):
    """Learning-health metrics — feedback coverage, weighted precision, FP rate."""
    return await engine.pg.metrics()


@router.get("/dashboard")
async def get_dashboard(request: Request, days: int = Query(14, ge=1, le=90),
                        engine: Engine = Depends(get_engine)):
    """后台总览:记忆库规模与构成、学习健康度、近期活动曲线、成熟记忆、最新进化事件、各能力是否就绪。"""
    s = await engine.pg.stats()
    facets = await engine.pg.enzyme_facets()
    mature = await engine.top_mature(limit=6)
    config = request.app.state.config
    return {
        "totals": {
            "enzymes": s["total_enzymes"],
            "avg_confidence": s["avg_confidence"],
            "triggers": s["total_triggers"],
            "activations": s["total_activations"],
            "vectors": (await engine.qdrant.get_stats()).get("documents", 0),
        },
        "by_status": facets["status"],
        "by_origin": facets["origin"],
        "by_severity": facets["severity"],
        "top_domains": dict(list(facets["domain"].items())[:8]),
        "metrics": await engine.pg.metrics(),
        "activity": await engine.pg.activity_series(days),
        "jobs": await engine.pg.job_counts(),
        "mature": [
            {"id": e.id, "patterns": e.patterns_readable, "then": e.then, "confidence": round(e.confidence, 3),
             "precision": round(e.precision, 3), "triggers": e.trigger_count}
            for e in mature
        ],
        "recent_events": await _get_recent_events(engine, 8),
        "capabilities": capabilities(config),
    }


@router.get("/relations/{enzyme_id}")
async def get_relations(
    enzyme_id: str, hops: int = Query(2, ge=1, le=5), limit: int = Query(20, ge=1, le=200),
    engine: Engine = Depends(get_engine),
):
    related = await engine.pg.get_related(enzyme_id, hops=hops, limit=limit)
    return {"enzyme_id": enzyme_id, "related": related}


@router.get("/graph")
async def get_graph(limit: int = Query(100, ge=1, le=2000), engine: Engine = Depends(get_engine)):
    async with engine.pg._pool.acquire() as conn:
        nodes_rows = await conn.fetch(
            """SELECT id, domain, confidence, trigger_count,
                      patterns, then_action, severity, tags, origin
               FROM enzymes ORDER BY confidence DESC LIMIT $1""",
            limit,
        )
        edges_rows = await conn.fetch(
            """SELECT source_id, target_id, relation, weight FROM enzyme_relations
               WHERE source_id IN (SELECT id FROM enzymes ORDER BY confidence DESC LIMIT $1)
               ORDER BY weight DESC LIMIT $2""",
            limit, limit * 3,
        )

    import json as _json

    nodes = []
    for r in nodes_rows:
        # Parse patterns JSONB → readable text
        patterns_raw = r["patterns"]
        if isinstance(patterns_raw, str):
            patterns_raw = _json.loads(patterns_raw)
        patterns_text = " / ".join(
            p.get("value", "") for p in (patterns_raw or []) if p.get("value")
        ) or r["id"][:12]

        nodes.append({
            "id": r["id"],
            "domain": r["domain"],
            "confidence": float(r["confidence"]),
            "trigger_count": r["trigger_count"],
            "patterns_text": patterns_text,
            "then": r["then_action"] or "",
            "severity": r["severity"] or "info",
            "tags": list(r["tags"]) if r["tags"] else [],
            "origin": r["origin"] or "manual",
        })

    edges = [
        {"source": r["source_id"], "target": r["target_id"],
         "relation": r["relation"], "weight": float(r["weight"])}
        for r in edges_rows
    ]
    return {"nodes": nodes, "edges": edges}


@router.get("/search")
async def search_enzymes(
    q: str = Query(..., min_length=1, max_length=2000),
    domain: str = "",
    limit: int = Query(20, ge=1, le=100),
    engine: Engine = Depends(get_engine),
):
    from app.llm_client import embed
    vector = await embed(q)
    if vector:
        hits = await engine.qdrant.search(vector, domain=domain or None, limit=limit)
    else:
        # Fallback: PG keyword search
        enzymes = await engine.pg.keyword_search([q], domain=domain or None, limit=limit)
        hits = [{"id": e.id, "then": e.then, "patterns_text": e.patterns_readable} for e in enzymes]
    return {"hits": hits, "total": len(hits)}


# ── Helpers ───────────────────────────────────────────────────

async def _get_recent_events(engine: Engine, limit: int) -> list[dict]:
    async with engine.pg._pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM evolution_log ORDER BY created_at DESC LIMIT $1", limit
        )
    return [event_row_to_dict(r) for r in rows]

