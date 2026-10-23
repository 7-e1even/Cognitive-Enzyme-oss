"""Immune Patrol — Microglia-inspired knowledge detoxification.

Detects and quarantines problematic enzymes:
1. Zombie: repeatedly triggered but almost always fails
2. Stale: old and never triggered (dead weight)
3. Contradiction: two enzymes give opposite advice for similar conditions

Brain science basis:
- Microglia: brain's resident immune cells, dual role in defense + pruning
- M1 mode (pro-inflammatory): detect and flag threats
- M2 mode (anti-inflammatory): repair and clean up
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from app.core import (
    Enzyme,
    EvolutionEvent,
    EventType,
)
from app.config import LLMConfig
from app.llm_client import chat_json, embed
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)


# ── Zombie Detection (pure SQL, no LLM) ─────────────────────

async def _detect_zombies(pg: PGStore) -> list[Enzyme]:
    """Find enzymes with high trigger count but very low precision."""
    all_enzymes = await pg.list_enzymes(min_confidence=0.0, limit=10000)
    return [
        e for e in all_enzymes
        if e.status == "active"
        and e.trigger_count >= 10
        and e.precision < 0.1
    ]


# ── Stale Detection (pure SQL, no LLM) ──────────────────────

async def _detect_stale(pg: PGStore, max_age_days: int = 180) -> list[Enzyme]:
    """Find enzymes that are old and genuinely unused — dead weight.

    "Unused" means never matched by prime() (activation_count == 0) AND never
    reported (trigger_count == 0). An enzyme matched hundreds of times but never
    reported is NOT stale — it is actively useful; quarantining it would be the
    classic mistake of confusing a starved feedback channel with dead knowledge.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    all_enzymes = await pg.list_enzymes(min_confidence=0.0, limit=10000)
    return [
        e for e in all_enzymes
        if e.status == "active"
        and e.trigger_count == 0
        and e.activation_count == 0
        and e.last_activated is None
        and e.created_at < cutoff
    ]


# ── Speculative Capacity Budget (pure SQL, no LLM) ──────────

async def _prune_speculative_overflow(pg: PGStore, budget: int = 200) -> list[Enzyme]:
    """Cap the pool of unvalidated speculative enzymes.

    dream/mutation now manufacture hypothesis enzymes — the project's original
    "active memory never accumulates junk" premise no longer holds. Without a
    ceiling, low-confidence speculation dilutes prime() recall forever (and the
    precision-based prune never fires because they're never reported).

    Speculative = active, machine-generated (mutated/evolved), never validated
    (trigger_count == 0), low confidence (< 0.35). When over budget, evict the
    least-useful first: never-activated before activated, then oldest use.
    """
    all_enzymes = await pg.list_enzymes(min_confidence=0.0, limit=10000)
    speculative = [
        e for e in all_enzymes
        if e.status == "active"
        and e.trigger_count == 0
        and e.confidence < 0.35
        and e.origin.value in ("mutated", "evolved")
    ]
    if len(speculative) <= budget:
        return []

    # Sort least-useful first: never-activated, then oldest last-used.
    def _last_used(e: Enzyme):
        return e.last_activated or e.created_at

    speculative.sort(key=lambda e: (e.activation_count, _last_used(e)))
    return speculative[: len(speculative) - budget]


# ── Contradiction Detection (uses LLM) ──────────────────────

async def _find_similar_pairs(
    pg: PGStore,
    qdrant: QdrantStore,
    embedding_config,
    limit: int = 10,
) -> list[tuple[Enzyme, Enzyme]]:
    """Find enzyme pairs with similar patterns but potentially conflicting advice."""
    active = await pg.list_enzymes(min_confidence=0.3, limit=200)
    active = [e for e in active if e.status == "active"]

    if len(active) < 2:
        return []

    pairs: list[tuple[Enzyme, Enzyme]] = []

    # For each enzyme, find its nearest neighbor via vector search
    for enzyme in active[:limit]:
        text = f"{enzyme.patterns_readable} {enzyme.then[:100]}"
        vector = await embed(text, config=embedding_config)
        if not vector:
            continue

        hits = await qdrant.search(vector, limit=3)
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            hit_id = hit.get("id", "")
            if hit_id and hit_id != enzyme.id:
                neighbor = await pg.get_enzyme(hit_id)
                if neighbor and neighbor.status == "active":
                    pairs.append((enzyme, neighbor))
                    break  # One neighbor per enzyme is enough

    return pairs


async def _check_contradiction(
    a: Enzyme, b: Enzyme, llm_config: LLMConfig
) -> dict | None:
    """Ask LLM if two similar enzymes are contradictory."""
    system = """你是一个知识一致性审核员。给你两条经验知识，判断它们是否矛盾。

规则：
1. "矛盾"是指：对于相似的触发条件，给出了相反或不兼容的建议
2. "互补"是两条知识从不同角度补充同一主题，不算矛盾
3. "无关"是两条知识虽然相似但讨论的是不同场景

返回 JSON:
{
  "relation": "contradiction" | "complementary" | "unrelated",
  "reason": "简要说明判断理由",
  "loser_index": 0 或 1（仅当 contradiction 时，哪条应该被淘汰，选质量更低的）
}"""

    user = f"""知识 0:
触发条件: {a.patterns_readable}
建议: {a.then}
理由: {a.because}
置信度: {a.confidence:.2f}, 精确度: {a.precision:.2f}, 触发次数: {a.trigger_count}

知识 1:
触发条件: {b.patterns_readable}
建议: {b.then}
理由: {b.because}
置信度: {b.confidence:.2f}, 精确度: {b.precision:.2f}, 触发次数: {b.trigger_count}"""

    result = await chat_json(system=system, user=user, config=llm_config)
    if not result or not isinstance(result, dict):
        return None
    if result.get("relation") != "contradiction":
        return None
    return result


async def _detect_contradictions(
    pg: PGStore,
    qdrant: QdrantStore,
    llm_config: LLMConfig,
    embedding_config=None,
    limit: int = 10,
) -> list[tuple[str, str, str]]:
    """Find contradictory enzyme pairs. Returns list of (loser_id, winner_id, reason)."""
    if not llm_config.available:
        return []

    pairs = await _find_similar_pairs(pg, qdrant, embedding_config, limit=limit)
    contradictions: list[tuple[str, str, str]] = []

    for a, b in pairs:
        try:
            result = await _check_contradiction(a, b, llm_config)
            if not result:
                continue

            loser_idx = result.get("loser_index", 0)
            loser = a if loser_idx == 0 else b
            winner = b if loser_idx == 0 else a
            contradictions.append((loser.id, winner.id, result.get("reason", "")))
            logger.info("Immune: contradiction found: %s ↔ %s → quarantine %s",
                        a.id, b.id, loser.id)

        except Exception:
            logger.exception("Immune: contradiction check failed for %s ↔ %s", a.id, b.id)

    return contradictions


# ── Main Patrol ──────────────────────────────────────────────

async def run_immune_patrol(
    pg: PGStore,
    qdrant: QdrantStore,
    sync: SyncManager,
    llm_config: LLMConfig,
    embedding_config=None,
) -> dict:
    """Run one immune patrol cycle: detect and quarantine problematic enzymes.

    Returns summary dict with counts.
    """
    results = {"zombies": 0, "stale": 0, "contradictions": 0, "speculative_pruned": 0}

    # 0. Speculative capacity budget (no LLM)
    overflow = await _prune_speculative_overflow(pg)
    for e in overflow:
        e.status = "quarantine"
        e.updated_at = datetime.now(timezone.utc)
        await sync.sync_enzyme(e, await pg.upsert_enzyme(e))
        await pg.log_event(EvolutionEvent(
            enzyme_id=e.id,
            event_type=EventType.IMMUNE,
            detail={
                "patrol_type": "speculative_overflow",
                "origin": e.origin.value,
                "activation_count": e.activation_count,
                "action": "quarantine",
            },
        ))
    results["speculative_pruned"] = len(overflow)

    # 1. Zombie detection (no LLM)
    zombies = await _detect_zombies(pg)
    for z in zombies:
        z.status = "quarantine"
        z.updated_at = datetime.now(timezone.utc)
        await sync.sync_enzyme(z, await pg.upsert_enzyme(z))
        await pg.log_event(EvolutionEvent(
            enzyme_id=z.id,
            event_type=EventType.IMMUNE,
            detail={
                "patrol_type": "zombie",
                "precision": round(z.precision, 3),
                "trigger_count": z.trigger_count,
                "action": "quarantine",
            },
        ))
    results["zombies"] = len(zombies)

    # 2. Stale detection (no LLM)
    stale = await _detect_stale(pg)
    for s in stale:
        s.status = "quarantine"
        s.updated_at = datetime.now(timezone.utc)
        await sync.sync_enzyme(s, await pg.upsert_enzyme(s))
        age_days = (datetime.now(timezone.utc) - s.created_at).days
        await pg.log_event(EvolutionEvent(
            enzyme_id=s.id,
            event_type=EventType.IMMUNE,
            detail={
                "patrol_type": "stale",
                "age_days": age_days,
                "action": "quarantine",
            },
        ))
    results["stale"] = len(stale)

    # 3. Contradiction detection (uses LLM if available)
    contradictions = await _detect_contradictions(
        pg, qdrant, llm_config, embedding_config, limit=10,
    )
    for loser_id, winner_id, reason in contradictions:
        loser = await pg.get_enzyme(loser_id)
        if loser and loser.status == "active":
            old_confidence = loser.confidence
            # Soft degradation: halve confidence instead of immediate quarantine
            loser.confidence *= 0.5
            action = "degraded"

            # Only quarantine if confidence drops below threshold
            if loser.confidence <= 0.1:
                loser.status = "quarantine"
                action = "quarantine"

            loser.updated_at = datetime.now(timezone.utc)
            await sync.sync_enzyme(loser, await pg.upsert_enzyme(loser))
            await pg.log_event(EvolutionEvent(
                enzyme_id=loser_id,
                event_type=EventType.IMMUNE,
                detail={
                    "patrol_type": "contradiction",
                    "conflicts_with": winner_id,
                    "reason": reason,
                    "action": action,
                    "confidence_before": round(old_confidence, 3),
                    "confidence_after": round(loser.confidence, 3),
                },
            ))
    results["contradictions"] = len(contradictions)

    total = sum(results.values())
    if total > 0:
        logger.info(
            "Immune patrol: %d zombies, %d stale, %d contradictions, %d speculative quarantined",
            results["zombies"], results["stale"], results["contradictions"],
            results["speculative_pruned"],
        )
    else:
        logger.debug("Immune patrol: all clear")

    return results
