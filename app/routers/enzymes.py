"""Enzymes CRUD router."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core import Enzyme, Pattern, PatternType, Severity
from app.core.engine import Engine
from app.dependencies import get_engine
from app.delivery.formatter import format_enzyme_detail
from app.routers._helpers import audit
from app.routers.auth import get_current_user
from app.schemas import ContributeRequest

router = APIRouter(prefix="/api/enzymes", tags=["enzymes"])
logger = logging.getLogger(__name__)

def _list_item(e: Enzyme) -> dict:
    return {
        "id": e.id,
        "type": e.type.value,
        "patterns": e.patterns_readable,
        "then": e.then,
        "because": e.because,
        "severity": e.severity.value,
        "confidence": round(e.confidence, 3),
        "precision": round(e.precision, 3),
        "trigger_count": e.trigger_count,
        "success_count": e.success_count,
        "false_positive_count": e.false_positive_count,
        "activation_count": e.activation_count,
        "origin": e.origin.value,
        "domain": e.domain,
        "tags": e.tags,
        "status": e.status,
        "quality_score": round(e.quality_score, 2),
        "exemplars": len(e.exemplars),
        "version": e.version,
        "last_activated": e.last_activated.isoformat() if e.last_activated else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def _parse_patterns(text: str) -> list[Pattern]:
    """"keyword1, keyword2, regex:xxx" → Pattern 列表。"""
    patterns = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("regex:"):
            patterns.append(Pattern(type=PatternType.REGEX, value=part[6:].strip()))
        else:
            patterns.append(Pattern(type=PatternType.KEYWORD, value=part))
    return patterns


@router.get("")
async def list_enzymes(
    q: str = "",
    domain: str = "",
    status: str = "",
    origin: str = "",
    severity: str = "",
    min_confidence: float = 0.0,
    sort: str = "confidence",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    engine: Engine = Depends(get_engine),
):
    """记忆列表:文本搜索 + 过滤 + 排序 + 分页;total 为满足条件的总数。"""
    enzymes, total = await engine.pg.query_enzymes(
        q=q.strip(), domain=domain, status=status, origin=origin, severity=severity,
        min_confidence=min_confidence, sort=sort, desc=order == "desc", limit=limit, offset=offset,
    )
    return {"enzymes": [_list_item(e) for e in enzymes], "total": total}


@router.get("/facets")
async def enzyme_facets(engine: Engine = Depends(get_engine)):
    """过滤器选项:各领域 / 状态 / 来源 / 严重度的记忆条数,外加领域表里的描述与来源(自动归类用)。"""
    facets: dict = await engine.pg.enzyme_facets()
    try:
        facets["domain_info"] = {d["name"]: {"description": d["description"], "source": d["source"]}
                                 for d in await engine.pg.list_domains()}
    except Exception as e:  # 领域表不可用时照旧只返回条数
        logger.warning("Domain table unavailable for facets: %s", e)
        facets["domain_info"] = {}
    return facets


class BulkAction(BaseModel):
    ids: list[str]
    action: Literal["delete", "activate", "quarantine", "reject"]


@router.post("/bulk")
async def bulk_action(body: BulkAction, user: dict = Depends(get_current_user),
                      engine: Engine = Depends(get_engine)):
    done, missing = [], []
    for enzyme_id in dict.fromkeys(body.ids):
        if body.action == "delete":
            (done if await engine.delete_enzyme(enzyme_id) else missing).append(enzyme_id)
            continue
        enzyme = await engine.get_enzyme(enzyme_id)
        if enzyme is None:
            missing.append(enzyme_id)
            continue
        enzyme.status = {"activate": "active", "quarantine": "quarantine", "reject": "rejected"}[body.action]
        if body.action == "activate":
            enzyme.confidence = max(enzyme.confidence, 0.4)
        await engine.update_enzyme(enzyme)
        done.append(enzyme_id)
    await audit(engine, user, f"enzyme.bulk_{body.action}", f"{len(done)} 条", {"ids": done})
    return {"action": body.action, "done": done, "missing": missing}


# ── Individual enzyme routes ──

@router.get("/{enzyme_id}")
async def get_enzyme(enzyme_id: str, engine: Engine = Depends(get_engine)):
    enzyme = await engine.get_enzyme(enzyme_id)
    if not enzyme:
        raise HTTPException(status_code=404, detail=f"Enzyme {enzyme_id} not found")
    related = await engine.pg.get_related(enzyme_id, limit=10)
    detail = format_enzyme_detail(enzyme, related)
    detail.update({
        "pattern_list": [{"type": p.type.value, "value": p.value} for p in enzyme.patterns],
        "status": enzyme.status,
        "quality_score": round(enzyme.quality_score, 2),
        "conditions": enzyme.conditions,
        "negations": enzyme.negations,
        "activation_count": enzyme.activation_count,
        "last_activated": enzyme.last_activated.isoformat() if enzyme.last_activated else None,
        "last_triggered": enzyme.last_triggered.isoformat() if enzyme.last_triggered else None,
        "created_at": enzyme.created_at.isoformat() if enzyme.created_at else None,
        "updated_at": enzyme.updated_at.isoformat() if enzyme.updated_at else None,
    })
    return detail


class PatternIn(BaseModel):
    type: Literal["keyword", "regex"] = "keyword"
    value: str


class EnzymeUpdate(BaseModel):
    patterns: list[PatternIn] | None = None
    then: str | None = None
    because: str | None = None
    domain: str | None = None
    tags: list[str] | None = None
    references: list[str] | None = None
    conditions: list[str] | None = None
    negations: list[str] | None = None
    severity: Severity | None = None
    confidence: float | None = Field(None, ge=0, le=1)
    status: Literal["active", "quarantine", "rejected"] | None = None


def _clean(items: list[str]) -> list[str]:
    return [x.strip() for x in items if x and x.strip()]


@router.put("/{enzyme_id}")
async def update_enzyme(enzyme_id: str, body: EnzymeUpdate, user: dict = Depends(get_current_user),
                        engine: Engine = Depends(get_engine)):
    """编辑记忆:未提供的字段保持不变。"""
    enzyme = await engine.get_enzyme(enzyme_id)
    if not enzyme:
        raise HTTPException(status_code=404, detail="Enzyme not found")
    fields = body.model_dump(exclude_unset=True)
    if "patterns" in fields and body.patterns is not None:
        patterns = [Pattern(type=PatternType(p.type), value=p.value.strip()) for p in body.patterns if p.value.strip()]
        if not patterns:
            raise HTTPException(status_code=422, detail="至少需要一个触发条件")
        enzyme.patterns = patterns
    if body.then is not None:
        if not body.then.strip():
            raise HTTPException(status_code=422, detail="建议内容不能为空")
        enzyme.then = body.then.strip()
    if body.because is not None:
        enzyme.because = body.because.strip()
    if body.domain is not None:
        enzyme.domain = body.domain.strip()
    for key in ("tags", "references", "conditions", "negations"):
        if fields.get(key) is not None:
            setattr(enzyme, key, _clean(fields[key]))
    if body.severity is not None:
        enzyme.severity = body.severity
    if body.confidence is not None:
        enzyme.confidence = body.confidence
    if body.status is not None:
        enzyme.status = body.status
    await engine.update_enzyme(enzyme)
    await audit(engine, user, "enzyme.update", enzyme_id, {"fields": sorted(fields)})
    return {"status": "updated", "id": enzyme_id}


@router.post("")
async def create_enzyme(req: ContributeRequest, user: dict = Depends(get_current_user),
                        engine: Engine = Depends(get_engine)):
    """Create a new enzyme from user input."""
    from app.core import Origin
    patterns = _parse_patterns(req.when)
    if not patterns:
        raise HTTPException(status_code=400, detail="至少需要一个触发条件")
    enzyme = await engine.create_enzyme(
        patterns=patterns, then_action=req.then, because=req.because,
        origin=Origin.MANUAL, domain=req.domain, tags=req.tags,
        references=req.references, severity=req.severity,
    )
    await audit(engine, user, "enzyme.create", enzyme.id, {"then": req.then[:200]})
    return {"id": enzyme.id, "status": "created", "confidence": enzyme.confidence}


@router.delete("/{enzyme_id}")
async def delete_enzyme(enzyme_id: str, user: dict = Depends(get_current_user),
                        engine: Engine = Depends(get_engine)):
    enzyme = await engine.get_enzyme(enzyme_id)
    ok = await engine.delete_enzyme(enzyme_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Enzyme not found")
    await audit(engine, user, "enzyme.delete", enzyme_id, {"then": enzyme.then[:200] if enzyme else ""})
    return {"status": "deleted", "id": enzyme_id}
