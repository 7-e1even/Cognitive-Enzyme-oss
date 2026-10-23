"""Crossover — merge co-triggered enzymes into composite enzymes.

LLM-enhanced: uses LLM to generate fused suggestions instead of mechanical text concatenation.
Falls back to rule-based merging if LLM is not configured.
"""

from __future__ import annotations

import logging

from app.core import (
    Enzyme,
    EnzymeRelation,
    EnzymeType,
    EvolutionEvent,
    EventType,
    Origin,
    Pattern,
    RelationType,
)
from app.storage.pg_store import PGStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)


async def find_crossover_candidates(pg: PGStore, min_weight: float = 2.0) -> list[tuple[str, str, float]]:
    """Find enzyme pairs with strong co-triggered relationships."""
    async with pg._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source_id, target_id, weight
            FROM enzyme_relations
            WHERE relation = 'co_triggered' AND weight >= $1
            ORDER BY weight DESC LIMIT 20
            """,
            min_weight,
        )
    return [(r["source_id"], r["target_id"], r["weight"]) for r in rows]


async def crossover(
    enzyme_a: Enzyme,
    enzyme_b: Enzyme,
    mode: str,
    pg: PGStore,
    sync: SyncManager,
) -> Enzyme | None:
    """Create a composite enzyme by merging two co-triggered enzymes."""
    if enzyme_a.domain and enzyme_b.domain and enzyme_a.domain != enzyme_b.domain:
        return None

    # Check duplicate
    all_enzymes = await pg.list_enzymes(limit=10000)
    for e in all_enzymes:
        if e.origin == Origin.CROSSED and enzyme_a.id in e.parent_ids and enzyme_b.id in e.parent_ids:
            return None

    if mode == "conditions_merge":
        child = _conditions_merge(enzyme_a, enzyme_b)
    elif mode == "action_merge":
        child = await _action_merge_llm(enzyme_a, enzyme_b)
        if not child:
            child = _action_merge_rule(enzyme_a, enzyme_b)
    else:
        return None

    if not child:
        return None

    from app.learning import known_filter
    if await known_filter.skip_enzyme(child):
        logger.info("Crossover [%s]: child already known to model, skipped", mode)
        return None

    await sync.sync_enzyme(child, await pg.upsert_enzyme(child))

    for pid in child.parent_ids:
        await pg.upsert_relation(EnzymeRelation(
            source_id=child.id, target_id=pid, relation=RelationType.EVOLVED_FROM,
        ))

    await pg.log_event(EvolutionEvent(
        enzyme_id=child.id,
        event_type=EventType.CROSSOVER,
        detail={"parent_a": enzyme_a.id, "parent_b": enzyme_b.id, "mode": mode},
    ))

    logger.info("Crossover [%s]: %s + %s → %s", mode, enzyme_a.id, enzyme_b.id, child.id)
    return child


async def run_crossover_cycle(pg: PGStore, sync: SyncManager, max_children: int = 3) -> dict:
    """Run one crossover cycle."""
    candidates = await find_crossover_candidates(pg)
    created = 0
    for a_id, b_id, weight in candidates:
        if created >= max_children:
            break
        enzyme_a = await pg.get_enzyme(a_id)
        enzyme_b = await pg.get_enzyme(b_id)
        if not enzyme_a or not enzyme_b:
            continue
        mode = "action_merge" if enzyme_a.precision > 0.7 and enzyme_b.precision > 0.7 else "conditions_merge"
        child = await crossover(enzyme_a, enzyme_b, mode, pg, sync)
        if child:
            created += 1
    return {"created": created, "candidates_checked": len(candidates)}


# ── Merge strategies ──────────────────────────────────────────

def _conditions_merge(a: Enzyme, b: Enzyme) -> Enzyme | None:
    """Merge conditions: combine patterns → more precise enzyme."""
    combined_patterns = list(a.patterns)
    existing = {p.value.lower() for p in a.patterns}
    for p in b.patterns:
        if p.value.lower() not in existing:
            combined_patterns.append(p)
            existing.add(p.value.lower())

    if len(combined_patterns) <= len(a.patterns):
        return None

    then = a.then if len(a.then) > len(b.then) else b.then
    because = a.because if len(a.then) > len(b.then) else b.because

    return Enzyme(
        type=EnzymeType.COMPOSITE,
        patterns=combined_patterns,
        conditions=list(set(a.conditions + b.conditions)),
        negations=list(set(a.negations + b.negations)),
        then=then,
        because=f"[条件合并] {because}",
        severity=a.severity if a.severity.value == "critical" else b.severity,
        references=list(set(a.references + b.references))[:5],
        confidence=0.5,
        origin=Origin.CROSSED,
        parent_ids=[a.id, b.id],
        domain=a.domain or b.domain,
        tags=list(set(a.tags + b.tags)),
    )


async def _action_merge_llm(a: Enzyme, b: Enzyme) -> Enzyme | None:
    """LLM-enhanced action merge: genuinely fuse two suggestions."""
    from app.llm_client import chat

    if a.then == b.then:
        return None

    prompt = f"""你是安全专家。请将以下两条安全检测规则的建议合并为一条更全面、更有逻辑的建议。

规则 A:
- 触发条件: {a.patterns_readable}
- 建议: {a.then}
- 原因: {a.because}

规则 B:
- 触发条件: {b.patterns_readable}
- 建议: {b.then}
- 原因: {b.because}

要求:
1. 合并后的建议应涵盖两条规则的核心要点
2. 避免重复，语言简洁
3. 如果两条建议有因果关系，按逻辑顺序组织
4. 直接给出合并后的建议内容，不要解释过程"""

    merged_text = await chat(
        system="你是安全领域专家，善于整合安全检测规则。",
        user=prompt,
    )

    if not merged_text or len(merged_text) < 10:
        return None

    # Combine patterns
    combined_patterns = list(a.patterns)
    existing = {p.value.lower() for p in a.patterns}
    for p in b.patterns:
        if p.value.lower() not in existing:
            combined_patterns.append(p)

    return Enzyme(
        type=EnzymeType.COMPOSITE,
        patterns=combined_patterns,
        conditions=list(set(a.conditions) & set(b.conditions)),
        negations=list(set(a.negations + b.negations)),
        then=merged_text,
        because=f"[LLM 合并] {a.because[:50]} + {b.because[:50]}",
        severity=a.severity if a.severity.value == "critical" else b.severity,
        references=list(set(a.references + b.references))[:5],
        confidence=0.5,
        origin=Origin.CROSSED,
        parent_ids=[a.id, b.id],
        domain=a.domain or b.domain,
        tags=list(set(a.tags + b.tags)),
    )


def _action_merge_rule(a: Enzyme, b: Enzyme) -> Enzyme | None:
    """Rule-based fallback: mechanical text concatenation."""
    if a.then == b.then:
        return None

    combined_patterns = list(a.patterns)
    existing = {p.value.lower() for p in a.patterns}
    for p in b.patterns:
        if p.value.lower() not in existing:
            combined_patterns.append(p)

    return Enzyme(
        type=EnzymeType.COMPOSITE,
        patterns=combined_patterns,
        conditions=list(set(a.conditions) & set(b.conditions)),
        negations=list(set(a.negations + b.negations)),
        then=f"{a.then}\n补充: {b.then}",
        because=f"[建议合并] {a.because} + {b.because}",
        severity=a.severity if a.severity.value == "critical" else b.severity,
        references=list(set(a.references + b.references))[:5],
        confidence=0.5,
        origin=Origin.CROSSED,
        parent_ids=[a.id, b.id],
        domain=a.domain or b.domain,
        tags=list(set(a.tags + b.tags)),
    )
