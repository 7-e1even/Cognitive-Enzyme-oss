"""语义去重 —— 巩固流程里用 Jev 判定"两条记忆是否给出实质相同的建议",合并近重复。

为什么需要:capture 持续沉淀新记忆,同一条经验会以不同措辞、不同领域反复出现。
近重复会(1)把 prime 的 choice 概率摊薄、(2)把学分分散到几条相同的记忆上,
拖慢自我改进。现行 merge_similar 要求触发词字面重叠 ≥80%、建议按空格分词重叠,
中文等于失效,且只在同领域内比较。

离线标定(tests/eval/jev_gate/run_dedup.py,语料向量最近 60 对人工标注):
merge_similar 找回 0/19 对重复;Jev τ=0.8 找回 16/19,误合并 0。合并是破坏性的
(被吸收的酶会被删除),所以阈值取保守侧,并把被吸收酶的完整快照写进演化日志以便审计/恢复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from app import settings
from app.config import TypeSafeConfig
from app.core import Enzyme, EnzymeRelation, EventType, EvolutionEvent, RelationType
from app.llm_client import embed
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager, _enzyme_text
from app.typesafe_client import _RULE_THEN_MAX, _RULE_WHY_MAX, system_one

logger = logging.getLogger(__name__)

# 与离线标定逐字一致——改措辞需重新标定阈值。
_DUP_QUESTION = "`规则A` 和 `规则B` 是否给出了实质相同的建议——在相同情境下,照其中任何一条做,效果都一样?"
_DUP_CRITERIA = {"true": "两条讲的是同一件事,合并成一条不会丢失任何建议",
                 "false": "建议不同、互补、适用条件不同或方向相反(即使主题相关)"}


def _rule(e: Enzyme) -> dict:
    # 与 typesafe_client._rule 同款截断:判官单次请求有 token 硬上限,只裁剪发给判官的内容。
    return {"触发": e.patterns_readable, "建议": e.then[:_RULE_THEN_MAX], "依据": e.because[:_RULE_WHY_MAX]}


def _snapshot(e: Enzyme) -> dict:
    return {"then": e.then, "because": e.because, "patterns": [p.value for p in e.patterns],
            "domain": e.domain, "confidence": e.confidence, "trigger_count": e.trigger_count,
            "success_count": e.success_count, "false_positive_count": e.false_positive_count,
            "exemplars": e.exemplars}


async def judge_duplicate(a: Enzyme, b: Enzyme, config: TypeSafeConfig) -> float | None:
    data = await system_one({"规则A": _rule(a), "规则B": _rule(b)},
                            {"dup": {"type": "noul", "instructions": _DUP_QUESTION, "criteria": _DUP_CRITERIA}},
                            config, timeout=30.0)
    try:
        return float(data["answers"]["dup"]["noul"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None


async def _candidate_pairs(active: list[Enzyme], qdrant: QdrantStore, neighbors: int) -> list[tuple[str, str]]:
    """近重复候选对:优先复用 Qdrant 里已存的向量(零 API 成本),缺的才补 embed。

    旧实现每条记忆一次串行 embed + 一次 search,万条库每次巩固就是上万次付费调用;
    现在批量取向量,只对索引缺失的条目回退 embed(并发 4,防远端限流)。"""
    ids = {e.id for e in active}
    pairs: set[tuple[str, str]] = set()

    vectors: dict[str, list[float]] = {}
    try:
        vectors = await qdrant.get_vectors([e.id for e in active])
    except Exception as e:
        logger.warning("Dedup vector fetch failed, falling back to embed: %s", e)

    missing = [e for e in active if e.id not in vectors]
    if missing:
        sem = asyncio.Semaphore(4)

        async def _embed_one(e: Enzyme) -> tuple[str, list[float] | None]:
            async with sem:
                return e.id, await embed(_enzyme_text(e), config=qdrant.embedding_config)

        for eid, vector in await asyncio.gather(*(_embed_one(e) for e in missing)):
            if vector:
                vectors[eid] = vector

    async def _neighbors_of(eid: str, vector: list[float]) -> None:
        for hit in await qdrant.search(vector, limit=neighbors + 1, include_all=True):
            hid = hit.get("id") if isinstance(hit, dict) else None
            if hid and hid != eid and hid in ids:
                pairs.add(tuple(sorted((eid, hid))))

    # search 是本地 Qdrant 读,并发 8 提速;远端限流只涉及上面的 embed 回退
    search_sem = asyncio.Semaphore(8)

    async def _bounded(eid: str, vector: list[float]) -> None:
        async with search_sem:
            await _neighbors_of(eid, vector)

    await asyncio.gather(*(_bounded(eid, v) for eid, v in vectors.items()))
    return sorted(pairs)


# ── 判定缓存:两条记忆内容和判官都没变,就沿用上次的判定概率 ──────────────────────────
# Jev 同一输入重判有约 ±0.01 的抖动(实测真实候选对 8 对里 1 对 0.09 / 0.10)。离阈值很远的判定,
# 抖动不可能改变合并与否,复用和重判结论必然一样;离阈值不到 _REJUDGE_MARGIN 的边界对每次照旧重判,
# 和原来每次巩固都重判的行为一致。
_REJUDGE_MARGIN = 0.05


def _pair_key(pair: tuple[str, str]) -> str:
    return f"{pair[0]}|{pair[1]}"


def _verdict_hash(a: Enzyme, b: Enzyme, config: TypeSafeConfig) -> str:
    """判定的全部输入:两条规则原文(按判定时的 A / B 顺序)、判官模型与服务地址、题面。任何一项变了就重新判。"""
    raw = json.dumps([_rule(a), _rule(b), config.model, config.api_url, _DUP_QUESTION, _DUP_CRITERIA],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


async def _load_verdicts(pg: PGStore, keys: list[str]) -> dict[str, tuple[str, float]]:
    try:
        return await pg.get_dedup_verdicts(keys)
    except Exception as e:  # 缓存读不到就全部重新判,结果不变
        logger.warning("Dedup verdict cache unavailable, judging all pairs: %s", e)
        return {}


async def _save_verdicts(pg: PGStore, rows: list[tuple[str, str, float]], keep: list[str]) -> None:
    try:
        await pg.save_dedup_verdicts(rows)
        await pg.prune_dedup_verdicts(keep)
    except Exception as e:
        logger.warning("Dedup verdict cache not saved: %s", e)


def _absorb(keeper: Enzyme, absorbed: Enzyme) -> None:
    """重复 = 同一条经验:触发词/出处/标签取并集,反馈证据相加(学习接着往下走,不从零开始)。"""
    existing = {p.value.lower() for p in keeper.patterns}
    for p in absorbed.patterns:
        if p.value.lower() not in existing:
            keeper.patterns.append(p)
            existing.add(p.value.lower())
    keeper.references += [r for r in absorbed.references if r not in keeper.references]
    keeper.negations += [n for n in absorbed.negations if n not in keeper.negations]
    # 经验样本也并过来:被吸收那条"见过的情境"同样是这条经验的适用情境
    keeper.exemplars = (keeper.exemplars + [x for x in absorbed.exemplars if x not in keeper.exemplars])[-20:]
    keeper.tags = sorted(set(keeper.tags) | set(absorbed.tags))
    keeper.trigger_count += absorbed.trigger_count
    keeper.success_count += absorbed.success_count
    keeper.false_positive_count += absorbed.false_positive_count
    if keeper.trigger_count:
        keeper.precision = keeper.success_count / keeper.trigger_count
    keeper.confidence = max(keeper.confidence, absorbed.confidence)
    keeper.version += 1
    keeper.updated_at = datetime.now(timezone.utc)


async def merge_semantic_duplicates(
    pg: PGStore,
    qdrant: QdrantStore,
    sync: SyncManager,
    typesafe_config: TypeSafeConfig,
    tau: float | None = None,
    neighbors: int = 3,
    concurrency: int = 6,
) -> dict:
    """找近邻候选对 → Jev 判重 → p ≥ tau 的按 p 从高到低贪心合并(每条每轮至多参与一次)。"""
    if not typesafe_config.available or not settings.get("learning.dedup"):
        return {"skipped": True}
    tau = tau if tau is not None else settings.get("learning.dedup_tau")
    active = [e for e in await pg.list_enzymes(limit=10000) if e.status == "active"]
    by_id = {e.id: e for e in active}
    pairs = await _candidate_pairs(active, qdrant, neighbors)

    keys = [_pair_key(p) for p in pairs]
    cached = await _load_verdicts(pg, keys)
    fresh: list[tuple[str, str, float]] = []
    reused = 0
    sem = asyncio.Semaphore(concurrency)

    async def judge(pair: tuple[str, str]) -> tuple[tuple[str, str], float | None]:
        nonlocal reused
        a, b = by_id[pair[0]], by_id[pair[1]]
        h = _verdict_hash(a, b, typesafe_config)
        hit = cached.get(_pair_key(pair))
        if hit and hit[0] == h and abs(hit[1] - tau) >= _REJUDGE_MARGIN:
            reused += 1
            return pair, hit[1]
        async with sem:
            p = await judge_duplicate(a, b, typesafe_config)
        if p is not None:   # 判失败的不记,下次重判
            fresh.append((_pair_key(pair), h, p))
        return pair, p

    judged = await asyncio.gather(*(judge(p) for p in pairs))
    await _save_verdicts(pg, fresh, keys)
    dups = sorted(((p, s) for p, s in judged if s is not None and s >= tau), key=lambda x: -x[1])

    merged: list[dict] = []
    touched: set[str] = set()
    for (a_id, b_id), p in dups:
        if a_id in touched or b_id in touched:
            continue
        a, b = by_id[a_id], by_id[b_id]
        keeper, absorbed = sorted((a, b), key=lambda e: (e.confidence, e.trigger_count), reverse=True)
        snapshot = _snapshot(absorbed)
        _absorb(keeper, absorbed)
        await sync.sync_enzyme(keeper, await pg.upsert_enzyme(keeper))
        await pg.upsert_relation(EnzymeRelation(
            source_id=keeper.id, target_id=absorbed.id, relation=RelationType.GENERALIZATION))
        await pg.log_event(EvolutionEvent(
            enzyme_id=keeper.id, event_type=EventType.CONSOLIDATION,
            detail={"action": "merge_semantic_duplicate", "absorbed_id": absorbed.id,
                    "dup_probability": round(p, 4), "absorbed_snapshot": snapshot}))
        await pg.delete_enzyme(absorbed.id)
        await sync.sync_delete(absorbed.id)
        touched |= {a_id, b_id}
        merged.append({"keeper": keeper.id, "absorbed": absorbed.id, "p": round(p, 3)})

    if merged:
        logger.info("Semantic dedup merged %d pairs (candidates=%d, tau=%.2f)", len(merged), len(pairs), tau)
    logger.info("Semantic dedup: %d candidate pairs, %d verdicts reused, %d judged now",
                len(pairs), reused, len(fresh))
    return {"candidates": len(pairs), "judged": sum(s is not None for _, s in judged),
            "reused_verdicts": reused, "merged": len(merged), "pairs": merged}
