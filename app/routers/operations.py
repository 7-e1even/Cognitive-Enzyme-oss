"""Data-plane router — prime, report, harvest, contribute, capture, recall(hook / agent / 园丁调用)。"""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.core import Origin, Pattern, PatternType
from app.core.engine import Engine
from app.core.features import project_slug
from app.dependencies import get_engine
from app.delivery.formatter import (
    format_episode_detail,
    format_episode_skeleton,
    format_match_results_slim,
)
from app.delivery.refractory import filter_refractory, mark_injections
from app.schemas import (
    PrimeRequest, ReportRequest, ReportBatchRequest, HarvestRequest, ContributeRequest,
    CaptureRequest, RecallRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["operations"])

# workspace 特征缓存:scan_workspace 走文件系统,不能每轮 prompt 都扫
_ws_cache: dict[str, tuple[float, dict]] = {}
_WS_TTL = 600.0
_WS_CACHE_MAX = 64


async def _workspace_features(cwd: str) -> dict | None:
    if not cwd:
        return None
    now = time.time()
    hit = _ws_cache.get(cwd)
    if hit and now - hit[0] < _WS_TTL:
        return hit[1] or None
    from app.core.features import scan_workspace
    try:
        feats = await asyncio.to_thread(scan_workspace, cwd)
    except Exception:
        logger.debug("scan_workspace failed (non-fatal)", exc_info=True)
        feats = {}
    if len(_ws_cache) >= _WS_CACHE_MAX:
        _ws_cache.clear()
    _ws_cache[cwd] = (now, feats)
    return feats or None


@router.post("/prime")
async def prime(req: PrimeRequest, engine: Engine = Depends(get_engine)):
    features = await _workspace_features(req.cwd)
    # 项目维度:显式 project(容器 CC 的 CE_PROJECT)优先,否则由 cwd 推导
    project = req.project.strip() or project_slug(req.cwd)
    results = await engine.match(
        req.context, domain=req.domain or None, max_results=req.max_results,
        features=features, project=project, session_id=req.session_id,
        source="failure" if req.source == "failure" else "prime",
    )
    # 不应期在记账前生效:被抑制的注入不产生激活信号(避免会话内自相关)
    results = await filter_refractory(engine.pg, req.session_id, results, seen_ids=req.seen_ids)
    if results:
        await engine.record_activations([r.enzyme.id for r in results])
    if len(results) > 1:
        await engine.record_co_triggers([r.enzyme.id for r in results[:10]])
    await mark_injections(engine.pg, req.session_id, [r.enzyme.id for r in results])
    await engine.record_session_context(req.session_id, req.context)
    # 每轮 prompt 都注入,是最 token 敏感的路径 → 用精简格式
    return {"matches": format_match_results_slim(results), "total": len(results)}


@router.post("/report")
async def report(req: ReportRequest, engine: Engine = Depends(get_engine)):
    expected = False if req.unexpected else None
    result = await engine.hebbian_update(
        enzyme_id=req.enzyme_id, success=req.success,
        expected=expected, detail=req.detail,
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    await engine.record_outcome(req.enzyme_id, "success" if req.success else "failure", "report",
                                decision_id=engine.last_decision(req.enzyme_id))
    return result


@router.post("/report_batch")
async def report_batch(req: ReportBatchRequest, engine: Engine = Depends(get_engine)):
    result = await engine.report_batch(
        enzyme_ids=req.enzyme_ids, success=req.success, detail=req.detail,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/harvest")
async def harvest(req: HarvestRequest, engine: Engine = Depends(get_engine)):
    result = await engine.harvest(
        insight=req.insight, context=req.context,
        domain=req.domain, tags=req.tags,
    )
    return result


@router.post("/contribute")
async def contribute(req: ContributeRequest, engine: Engine = Depends(get_engine)):
    patterns = []
    for part in req.when.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("regex:"):
            patterns.append(Pattern(type=PatternType.REGEX, value=part[6:].strip()))
        else:
            patterns.append(Pattern(type=PatternType.KEYWORD, value=part))
    if not patterns:
        raise HTTPException(status_code=400, detail="至少需要一个触发条件")
    enzyme = await engine.create_enzyme(
        patterns=patterns, then_action=req.then, because=req.because,
        origin=Origin.MANUAL, domain=req.domain, tags=req.tags,
        references=req.references, severity=req.severity,
    )
    return {"id": enzyme.id, "status": "created", "confidence": enzyme.confidence}


@router.post("/capture")
async def capture(req: CaptureRequest, engine: Engine = Depends(get_engine)):
    """Persist then asynchronously process a transcript without blocking hooks."""
    if not req.transcript.strip():
        return {"status": "empty"}

    project = req.project.strip() or project_slug(req.cwd)

    job_id = await engine.pg.enqueue_job(
        "capture",
        {
            "transcript": req.transcript,
            "domain": req.domain,
            "session_id": req.session_id,
            "source": req.source,
            "project": project,
        },
    )
    return {"status": "accepted", "job_id": job_id}


@router.post("/recall")
async def recall(req: RecallRequest, engine: Engine = Depends(get_engine)):
    """情景回想(模式补全):拿手头的残缺线索,把历史上最像的几幕调回来。

    与 /prime 的区别:prime 返回提炼后的规则("该做什么"),recall 返回带出处的
    完整一幕("当时发生了什么"),供 agent 主动回忆与类比。
    """
    from app.learning.episodes import cues_from_text

    cues = list(req.cues or [])
    if req.query:
        cues += cues_from_text(req.query)
    if not cues:
        raise HTTPException(status_code=400, detail="需要 cues 或 query 至少一项")

    hits = await engine.pg.recall_episodes(
        cues, domain=req.domain or None, limit=max(1, min(req.limit, 20)),
    )
    return {
        "episodes": [format_episode_skeleton(ep, overlap) for ep, overlap in hits],
        "total": len(hits),
        "cues_used": [c.strip().lower() for c in cues if c.strip()][:24],
    }


@router.get("/episode/{episode_id}")
async def episode_detail(episode_id: str, engine: Engine = Depends(get_engine)):
    """回想的第二层:钻取整幕(事件链/实体/报错签名/原文摘录)。"""
    episode = await engine.pg.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404, detail="episode not found")
    return format_episode_detail(episode)
