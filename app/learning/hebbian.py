"""Hebbian learning — prediction error layered updates.

Already integrated into engine.py. This module provides standalone utilities
for more complex Hebbian scenarios (batch updates, decay schedules).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from app.config import LLMConfig
from app.core import Enzyme, EvolutionEvent, EventType
from app.storage.pg_store import PGStore

logger = logging.getLogger(__name__)

MAX_NEGATIONS = 8  # cap to keep an enzyme from over-suppressing itself


def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ── Negative learning (suppression) ─────────────────────────────

async def extract_negation(
    enzyme: Enzyme,
    detail: str,
    llm_config: LLMConfig | None = None,
) -> str | None:
    """On corrective feedback, learn a discriminating term that should SUPPRESS
    this enzyme — i.e. a false-positive marker.

    Returns a short string to add to ``enzyme.negations`` (checked as a
    substring of future contexts by scorer.check_conditions), or None.

    LLM-gated on purpose: a wrong negation silently blinds the enzyme, so when
    no LLM is available or the model is unsure we add nothing rather than guess.
    """
    detail = (detail or "").strip()
    if not detail or len(detail) < 4:
        return None

    cfg = llm_config or LLMConfig()
    if not cfg.available:
        return None

    from app.llm_client import chat_json

    system = (
        "你在帮助一个安全直觉系统学会『何时不该触发』。给你一条直觉规则和它这次误报的说明，"
        "请找出一个能区分『这次误报场景』与『真正该触发场景』的关键词或短语——"
        "当未来上下文里出现这个词时，这条规则就应当被抑制。\n"
        "规则：\n"
        "1. 该词必须能在类似上下文里复现（具体技术名/API/框架/配置），不要泛词\n"
        "2. 不能等同于规则本身的触发词（那会让规则永不触发）\n"
        "3. 不确定就返回 null，宁缺毋滥\n"
        '返回 JSON: {"negation": "词或短语" 或 null, "confidence": 0到1}'
    )
    user = (
        f"规则触发词: {enzyme.patterns_readable}\n"
        f"规则建议: {enzyme.then}\n"
        f"本次误报说明: {detail}"
    )
    result = await chat_json(system=system, user=user, config=cfg)
    if not isinstance(result, dict):
        return None

    neg = result.get("negation")
    conf = result.get("confidence", 0)
    if not neg or not isinstance(neg, str):
        return None
    neg = neg.strip()
    if not neg or len(neg) > 60 or float(conf or 0) < 0.6:
        return None

    # Don't negate your own trigger — that would silence the enzyme entirely.
    own = {p.value.lower() for p in enzyme.patterns}
    if neg.lower() in own:
        return None
    if neg.lower() in {n.lower() for n in enzyme.negations}:
        return None
    return neg


async def batch_decay(pg: PGStore, decay_amount: float = 0.05) -> int:
    """Utility-based decay: only weaken enzymes that have been triggered
    but performed poorly AND haven't been triggered recently.

    Design rationale (SEDM-inspired):
    - Never-triggered enzymes are NOT decayed — they may simply not have
      encountered the right scenario yet, and decaying them would be
      punishing ignorance rather than poor performance.
    - Triggered-but-ineffective enzymes that haven't fired in 90+ days
      are decayed — evidence suggests they are low-utility.
    """
    enzymes = await pg.list_enzymes(limit=10000)
    decayed = 0
    now = datetime.now(timezone.utc)
    stale_threshold = timedelta(days=90)

    for enzyme in enzymes:
        # Skip non-active enzymes (quarantine/rejected)
        if enzyme.status != "active":
            continue
        # Skip never-triggered enzymes — no evidence to decay
        if enzyme.trigger_count == 0:
            continue
        # Skip high-performance enzymes
        if enzyme.precision >= 0.5:
            continue
        # Skip recently-used enzymes — "used" = reported (last_triggered) OR
        # matched by prime() (last_activated). Decay only targets enzymes that
        # are both low-precision AND have gone quiet on both channels.
        last_used = max(
            [t for t in (enzyme.last_triggered, enzyme.last_activated) if t is not None],
            default=None,
        )
        if last_used is None:
            # 两个时间戳都为空(如外部导入的数据):没有"沉寂 90 天"的证据,不衰减
            continue
        if (now - last_used) < stale_threshold:
            continue
        # Skip already very low confidence
        if enzyme.confidence <= 0.1:
            continue

        before_conf = enzyme.confidence
        enzyme.confidence = clamp(enzyme.confidence - decay_amount, 0.05, 1.0)
        enzyme.updated_at = now
        await pg.upsert_enzyme(enzyme)

        await pg.log_event(EvolutionEvent(
            enzyme_id=enzyme.id,
            event_type=EventType.CONSOLIDATION,
            detail={
                "action": "decay",
                "reason": f"triggered {enzyme.trigger_count}x, precision {enzyme.precision:.2f}, stale {stale_threshold.days}d+",
                "before": before_conf,
                "after": enzyme.confidence,
            },
        ))
        decayed += 1

    if decayed:
        logger.info("Batch decay: %d enzymes decayed by %.2f (utility-based)", decayed, decay_amount)
    return decayed


async def prune_low_precision(
    pg: PGStore,
    min_triggers: int = 30,
    max_precision: float = 0.1,
) -> int:
    """Mark extremely low-precision enzymes as dormant instead of deleting.

    Design rationale (ExpeL/Memento-inspired):
    - AI doesn't need to forget — it can keep low-value memories but
      deprioritize them. Deletion is irreversible and wasteful.
    - Enzymes are marked dormant (confidence → 0.05) rather than deleted.
    - prime() natural ranking will exclude them since confidence is near-zero.
    - Thresholds raised to min_triggers=30, max_precision=0.1 for safety.
    """
    enzymes = await pg.list_enzymes(limit=10000)
    dormant_count = 0

    for enzyme in enzymes:
        if enzyme.status != "active":
            continue
        if "dormant" in enzyme.tags and enzyme.confidence <= 0.05:
            continue  # 已是 dormant:不重复打标,否则每轮巩固都刷一条重复事件
        if enzyme.trigger_count >= min_triggers and enzyme.precision < max_precision:
            await pg.log_event(EvolutionEvent(
                enzyme_id=enzyme.id,
                event_type=EventType.CONSOLIDATION,
                detail={
                    "action": "dormant",
                    "reason": f"precision {enzyme.precision:.2f} < {max_precision} after {enzyme.trigger_count} triggers",
                    "previous_confidence": enzyme.confidence,
                },
            ))
            # Mark dormant instead of deleting
            enzyme.confidence = 0.05
            enzyme.tags = list(set(enzyme.tags + ["dormant"]))
            enzyme.updated_at = datetime.now(timezone.utc)
            await pg.upsert_enzyme(enzyme)
            dormant_count += 1

    if dormant_count:
        logger.info("Marked %d low-precision enzymes as dormant (not deleted)", dormant_count)
    return dormant_count
