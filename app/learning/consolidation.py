"""Consolidation — full memory consolidation pipeline.

Runs:
1. Decay untriggered enzymes
2. Prune low-precision enzymes
3. Prune failed mutants
4. Merge similar enzymes (generalization)
5. Run mutation cycle
6. Run crossover cycle
7. Dream cycle (DMN creative recombination)
8. Immune patrol (Microglia detoxification)
9. Regenerate Rules file
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

from app.core import (
    Enzyme,
    EnzymeRelation,
    EvolutionEvent,
    EventType,
    RelationType,
)
from app.delivery.rules_writer import generate_rules_content, write_rules
from app.learning.hebbian import batch_decay, prune_low_precision
from app.learning.mutation import run_mutation_cycle, prune_failed_mutants
from app.learning.crossover import run_crossover_cycle
from app.learning.dream import run_dream_cycle
from app.learning.immune import run_immune_patrol
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)


def _then_similarity(a_then: str, b_then: str) -> float:
    """Lightweight keyword-overlap similarity for 'then' (suggestion) fields.

    No embedding model needed — just checks if the suggestions talk about
    the same concepts. Returns 0.0-1.0.
    """
    words_a = {w.lower() for w in a_then.split() if len(w) > 2}
    words_b = {w.lower() for w in b_then.split() if len(w) > 2}
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


async def merge_similar(
    pg: PGStore,
    sync: SyncManager,
    pattern_threshold: float = 0.8,
    then_threshold: float = 0.4,
) -> int:
    """Merge enzymes with overlapping patterns AND similar suggestions.

    Design rationale (SEDM/A-MEM inspired):
    - Two enzymes with similar patterns but DIFFERENT suggestions should
      NOT be merged — they represent distinct insights.
    - Only merge when both patterns AND suggestions are similar.
    - If patterns overlap but suggestions differ, create a CO_TRIGGERED
      link instead (preserving both).

    Optimization: group by domain first to avoid O(n²) global scan.
    """
    all_enzymes = await pg.list_enzymes(limit=10000)
    # Only compare active enzymes
    active = [e for e in all_enzymes if e.status == "active"]

    # Group by domain — only compare within same domain
    by_domain: dict[str, list[Enzyme]] = defaultdict(list)
    for e in active:
        by_domain[e.domain].append(e)

    merged = 0
    linked = 0
    merged_ids: set[str] = set()

    for domain, enzymes in by_domain.items():
        # 关键词集合每条只算一次(原实现内层每对都重建);倒排索引剪枝:
        # 没有共同关键词的对比 overlap 必为 0,域内大库不再是全量 O(n²)。
        kw_sets = [{p.value.lower() for p in e.patterns} for e in enzymes]
        by_keyword: dict[str, list[int]] = defaultdict(list)
        for idx, kws in enumerate(kw_sets):
            for kw in kws:
                by_keyword[kw].append(idx)
        candidate_pairs: set[tuple[int, int]] = set()
        for idxs in by_keyword.values():
            for x in range(len(idxs)):
                for y in range(x + 1, len(idxs)):
                    candidate_pairs.add((idxs[x], idxs[y]))

        cpu_ticks = 0
        for i, j in sorted(candidate_pairs):  # 保持原全对比的 (i,j) 顺序,贪心语义不变
            a, b = enzymes[i], enzymes[j]
            if a.id in merged_ids or b.id in merged_ids:
                continue

            kw_a, kw_b = kw_sets[i], kw_sets[j]
            if not kw_a or not kw_b:
                continue
            overlap = len(kw_a & kw_b) / min(len(kw_a), len(kw_b))

            cpu_ticks += 1
            if cpu_ticks % 200 == 0:
                await asyncio.sleep(0)  # 大域纯 CPU 段让出事件循环,别卡住在线请求

            if overlap >= pattern_threshold:
                # Check if suggestions are also similar
                then_sim = _then_similarity(a.then, b.then)

                if then_sim >= then_threshold:
                    # Truly similar — merge
                    keeper, absorbed = (a, b) if a.confidence >= b.confidence else (b, a)

                    existing = {p.value.lower() for p in keeper.patterns}
                    for p in absorbed.patterns:
                        if p.value.lower() not in existing:
                            keeper.patterns.append(p)
                            existing.add(p.value.lower())

                    existing_refs = set(keeper.references)
                    for ref in absorbed.references:
                        if ref not in existing_refs:
                            keeper.references.append(ref)

                    keeper.tags = list(set(keeper.tags + absorbed.tags))
                    keeper.confidence = min(1.0, keeper.confidence + 0.05)
                    keeper.version += 1
                    keeper.updated_at = datetime.now(timezone.utc)

                    await sync.sync_enzyme(keeper, await pg.upsert_enzyme(keeper))

                    await pg.upsert_relation(EnzymeRelation(
                        source_id=keeper.id,
                        target_id=absorbed.id,
                        relation=RelationType.GENERALIZATION,
                    ))

                    await pg.log_event(EvolutionEvent(
                        enzyme_id=keeper.id,
                        event_type=EventType.CONSOLIDATION,
                        detail={
                            "action": "merge_similar",
                            "absorbed_id": absorbed.id,
                            "pattern_overlap": round(overlap, 2),
                            "then_similarity": round(then_sim, 2),
                        },
                    ))

                    await pg.delete_enzyme(absorbed.id)
                    await sync.sync_delete(absorbed.id)
                    merged_ids.add(absorbed.id)
                    # 吸收后 keeper 的关键词集合变大,后续对比要用新集合(与原实现一致)
                    kw_sets[i] = {p.value.lower() for p in enzymes[i].patterns}
                    kw_sets[j] = {p.value.lower() for p in enzymes[j].patterns}
                    merged += 1
                    logger.debug("Merged %s into %s (overlap=%.2f, then_sim=%.2f)",
                                 absorbed.id, keeper.id, overlap, then_sim)
                else:
                    # Patterns similar but suggestions differ — link, don't merge
                    await pg.upsert_relation(EnzymeRelation(
                        source_id=a.id,
                        target_id=b.id,
                        relation=RelationType.CO_TRIGGERED,
                    ))
                    linked += 1
                    logger.debug("Linked %s ↔ %s (patterns overlap=%.2f but then_sim=%.2f)",
                                 a.id, b.id, overlap, then_sim)

    if merged:
        logger.info("Merged %d similar enzyme pairs", merged)
    if linked:
        logger.info("Linked %d enzyme pairs with similar patterns but different suggestions", linked)
    return merged


async def full_consolidation(
    pg: PGStore,
    qdrant: QdrantStore,
    rules_path: str = "",
    config=None,
) -> dict:
    """Run the complete consolidation pipeline.

    Returns a summary dict with counts for each action.
    Each step is independently protected — a single step failure
    won't crash the entire pipeline.
    """
    sync = SyncManager(pg, qdrant)
    results: dict = {}
    errors: list[str] = []

    # 1. Decay
    try:
        results["decayed"] = await batch_decay(pg)
    except Exception as e:
        logger.exception("Consolidation step 1 (decay) failed")
        errors.append(f"decay: {e}")

    # 2. Mark low-precision enzymes as dormant (no deletion)
    try:
        dormant = await prune_low_precision(pg)
        results["dormant_count"] = dormant
    except Exception as e:
        logger.exception("Consolidation step 2 (prune low precision) failed")
        errors.append(f"prune_low_precision: {e}")

    # 3. Prune failed mutants
    try:
        results["pruned_mutants"] = await prune_failed_mutants(pg, sync)
    except Exception as e:
        logger.exception("Consolidation step 3 (prune mutants) failed")
        errors.append(f"prune_mutants: {e}")

    # 4. Merge similar
    try:
        results["merged_similar"] = await merge_similar(pg, sync)
    except Exception as e:
        logger.exception("Consolidation step 4 (merge similar) failed")
        errors.append(f"merge_similar: {e}")

    # Steps 4b-8 share runtime config; 5-8 are gated by schedule toggles
    from app.routers.schedule import schedule
    from app.config import Config
    runtime_config = config or Config.from_env()

    # 4b. Semantic dedup (Jev 判重;merge_similar 对中文与跨领域近重复基本失效)
    try:
        if runtime_config.typesafe.available:
            from app.learning.dedup import merge_semantic_duplicates
            dedup = await merge_semantic_duplicates(pg, qdrant, sync, runtime_config.typesafe)
            results["merged_semantic"] = dedup.get("merged", 0)
    except Exception as e:
        logger.exception("Consolidation step 4b (semantic dedup) failed")
        errors.append(f"semantic_dedup: {e}")

    # 4c. 小领域归并(领域自动归类的收尾:零散的小领域,成员归得进别的领域就并过去)
    try:
        if runtime_config.typesafe.available:
            from app.learning.domains import merge_small_domains
            results["moved_domain"] = await merge_small_domains(pg, sync, runtime_config.typesafe)
    except Exception as e:
        logger.exception("Consolidation step 4c (merge small domains) failed")
        errors.append(f"merge_small_domains: {e}")

    # 5. Mutation cycle (experimental, off by default)
    try:
        if schedule.mutation_enabled:
            mutation_result = await run_mutation_cycle(pg, sync)
            results["mutations_created"] = mutation_result["created"]
            results["mutations_pruned"] = mutation_result["pruned"]
    except Exception as e:
        logger.exception("Consolidation step 5 (mutation) failed")
        errors.append(f"mutation: {e}")

    # 6. Crossover cycle (experimental, off by default)
    try:
        if schedule.crossover_enabled:
            crossover_result = await run_crossover_cycle(pg, sync)
            results["crossovers_created"] = crossover_result["created"]
    except Exception as e:
        logger.exception("Consolidation step 6 (crossover) failed")
        errors.append(f"crossover: {e}")

    # 7. Dream cycle (DMN creative recombination)
    try:
        if schedule.dreamglia_enabled and runtime_config.llm.available:
            dream_result = await run_dream_cycle(
                pg, qdrant, sync, runtime_config.llm,
                max_dreams=schedule.dreamglia_max_dreams,
            )
            results.update(dream_result)
    except Exception as e:
        logger.exception("Consolidation step 7 (dream) failed")
        errors.append(f"dream: {e}")

    # 8. Immune patrol (Microglia detoxification)
    try:
        if schedule.dreamglia_enabled:
            immune_result = await run_immune_patrol(
                pg, qdrant, sync, runtime_config.llm,
                embedding_config=runtime_config.embedding,
            )
            results.update(immune_result)
    except Exception as e:
        logger.exception("Consolidation step 8 (immune) failed")
        errors.append(f"immune: {e}")

    # 8.5 Episode replay(离线「应用/测试」:拿历史情景考现有酶,给探索槽
    #     假设与低反馈酶判卷。放在 Rules 重生成之前,让最新判分进入 Rules)
    try:
        if schedule.replay_enabled and runtime_config.llm.available:
            from app.core.engine import Engine
            from app.learning.replay import run_replay_cycle
            replay_engine = Engine(runtime_config, pg, qdrant)
            results["replay"] = await run_replay_cycle(
                replay_engine, max_episodes=schedule.replay_max_episodes,
            )
    except Exception as e:
        logger.exception("Consolidation step 8.5 (replay) failed")
        errors.append(f"replay: {e}")

    # 9. Regenerate Rules
    try:
        mature = await pg.top_mature(limit=10)
        rules_content = generate_rules_content(mature)
        write_rules(rules_path, rules_content)
        results["rules_enzymes"] = len(mature)
    except Exception as e:
        logger.exception("Consolidation step 9 (rules) failed")
        errors.append(f"rules: {e}")

    # Final sync (best-effort)
    try:
        await sync.full_sync()
    except Exception as e:
        logger.exception("Consolidation final sync failed")
        errors.append(f"final_sync: {e}")

    try:
        total_enzymes = await pg.count_enzymes()
        results["total_enzymes"] = total_enzymes
    except Exception as e:
        logger.warning("Could not count enzymes: %s", e)

    if errors:
        results["errors"] = errors
        logger.warning("Consolidation completed with %d errors: %s",
                        len(errors), errors)
    else:
        logger.info("Consolidation complete: %s", results)
    return results
