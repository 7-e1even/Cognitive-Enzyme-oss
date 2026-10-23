"""Dream Engine — DMN-inspired creative recombination of enzymes.

During consolidation, randomly picks enzyme pairs from different domains
and uses LLM to attempt creative combination. Successful combinations
become hypothesis enzymes (low confidence) that need real-world validation.

Brain science basis:
- Default Mode Network (DMN) creates novel connections during rest
- Random recombination + selection = core of biological creativity
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from app.core import (
    Enzyme,
    EnzymeRelation,
    EvolutionEvent,
    EventType,
    Origin,
    Pattern,
    RelationType,
)
from app.config import LLMConfig
from app.learning import known_filter
from app.llm_client import chat_json
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)


async def _pick_random_pairs(
    pg: PGStore, count: int = 2
) -> list[tuple[Enzyme, Enzyme]]:
    """Pick random enzyme pairs, preferring cross-domain combinations."""
    all_enzymes = await pg.list_enzymes(min_confidence=0.3, limit=500)
    active = [e for e in all_enzymes if e.status == "active"]

    if len(active) < 2:
        return []

    # Group by domain
    by_domain: dict[str, list[Enzyme]] = {}
    for e in active:
        by_domain.setdefault(e.domain or "_default", []).append(e)

    pairs: list[tuple[Enzyme, Enzyme]] = []
    domains = list(by_domain.keys())

    for _ in range(count):
        if len(domains) >= 2:
            # Cross-domain: pick from two different domains
            d1, d2 = random.sample(domains, 2)
            a = random.choice(by_domain[d1])
            b = random.choice(by_domain[d2])
        else:
            # Same domain: just pick two different enzymes
            a, b = random.sample(active, 2)

        if a.id != b.id:
            pairs.append((a, b))

    return pairs


async def _llm_creative_combine(
    a: Enzyme, b: Enzyme, llm_config: LLMConfig
) -> dict | None:
    """Ask LLM to evaluate if two enzymes can be creatively combined."""
    system = """你是一个知识创新引擎。给你两条经验知识，请尝试将它们组合成一个新的、有用的洞察。

规则：
1. 新洞察必须同时用到两条知识的核心要素
2. 不是简单拼接，而是产生新的、非显而易见的认识
3. 如果组合没有意义，直接返回 {"plausible": false}

返回 JSON:
{
  "plausible": true/false,
  "insight": "新的洞察（简洁有力，一句话）",
  "scenario": "在什么场景下这个组合有用",
  "reasoning": "为什么这个组合有价值"
}"""

    user = f"""知识 A: {a.then}
背景 A: {a.because}

知识 B: {b.then}
背景 B: {b.because}"""

    result = await chat_json(system=system, user=user, config=llm_config)
    if not result or not isinstance(result, dict):
        return None
    if not result.get("plausible"):
        return None
    return result


def _merge_patterns(a: Enzyme, b: Enzyme) -> list[Pattern]:
    """Take representative patterns from both parents."""
    patterns: list[Pattern] = []
    seen: set[str] = set()

    for enzyme in [a, b]:
        for p in enzyme.patterns[:2]:  # Max 2 from each parent
            key = p.value.lower()
            if key not in seen:
                patterns.append(Pattern(type=p.type, value=p.value, weight=0.5))
                seen.add(key)

    return patterns or [Pattern(value="dream_hypothesis")]


async def run_dream_cycle(
    pg: PGStore,
    qdrant: QdrantStore,
    sync: SyncManager,
    llm_config: LLMConfig,
    max_dreams: int = 2,
) -> dict:
    """Run one dream cycle: attempt creative enzyme recombination.

    Returns summary dict with counts.
    """
    if not llm_config.available:
        logger.debug("Dream: LLM not configured, skipping")
        return {"dreams_created": 0, "dreams_tried": 0, "dreams_known": 0}

    pairs = await _pick_random_pairs(pg, count=max_dreams)
    if not pairs:
        return {"dreams_created": 0, "dreams_tried": 0, "dreams_known": 0}

    created = 0
    known = 0
    for a, b in pairs:
        try:
            combo = await _llm_creative_combine(a, b, llm_config)
            if not combo:
                continue

            patterns = _merge_patterns(a, b)
            enzyme = Enzyme(
                patterns=patterns,
                then=combo["insight"],
                because=f"白日梦组合: [{a.then[:40]}] × [{b.then[:40]}]",
                origin=Origin.EVOLVED,
                confidence=0.2,
                parent_ids=[a.id, b.id],
                tags=["dream_hypothesis"],
            )
            if await known_filter.skip_enzyme(enzyme):
                known += 1
                logger.info("Dream: hypothesis skipped (model already knows): %s", enzyme.then[:60])
                continue
            await sync.sync_enzyme(enzyme, await pg.upsert_enzyme(enzyme))

            # Link to parents
            for pid in [a.id, b.id]:
                await pg.upsert_relation(EnzymeRelation(
                    source_id=enzyme.id,
                    target_id=pid,
                    relation=RelationType.EVOLVED_FROM,
                ))

            await pg.log_event(EvolutionEvent(
                enzyme_id=enzyme.id,
                event_type=EventType.DREAM,
                detail={
                    "parents": [a.id, b.id],
                    "parent_actions": [a.then[:80], b.then[:80]],
                    "insight": combo["insight"],
                    "scenario": combo.get("scenario", ""),
                    "reasoning": combo.get("reasoning", ""),
                },
            ))
            created += 1
            logger.info("Dream: created hypothesis %s from %s × %s",
                        enzyme.id, a.id, b.id)

        except Exception:
            logger.exception("Dream: failed combining %s × %s", a.id, b.id)

    logger.info("Dream cycle: %d/%d combinations created, %d already-known", created, len(pairs), known)
    return {"dreams_created": created, "dreams_tried": len(pairs), "dreams_known": known}
