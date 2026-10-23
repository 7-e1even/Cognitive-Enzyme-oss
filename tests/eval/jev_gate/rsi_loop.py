"""闭环自我改进(RSI)模拟 —— 真实 Engine 代码跑"注入 → 反馈 → hebbian 学习"多轮,
看留出集上的召回质量随轮次如何演化,对比启用/不启用 Stage 4.5(Jev)。

- 查询按 id 哈希确定性切成 train(70%)/ test(30%);只在 train 上给反馈,test 只评估。
- 反馈 = 模拟 agent 调 report_batch(MCP report 的批量形态):批次里有任何相关记忆就
  报 success,否则 failure;信用按 report_batch 原逻辑(首位全额、其余按名次衰减回声)。
  这正是 N→1 归因问题:排第一的若不相关、却因同批的相关条目报了 success,就会被错奖。
- 记账"信用方向正确率":奖励落在相关记忆 / 惩罚落在不相关记忆 = 正确,按 credit 加权。
- --cold 0.25:把 25% 记忆的 confidence 压到 0.2(新生/被误伤),低于 hook 门槛 0.25,
  考察系统能否把它们重新学起来(冷启动死锁)。
- --harmful 0.15:15% 记忆设为"建议有误"——语义上适用(Jev 照样判适用),但 agent 照做
  会失败。系统必须从反馈里学会不信任它们;test 上记"有害暴露率"(注入里含有害记忆的比例)。
- --noise 0.1:agent 反馈以 10% 概率报反。

需要:本地 eval 栈(见 README「真实链路对照」)。每个 arm 开始前把语料状态重置。
  .venv-eval/bin/python -m tests.eval.jev_gate.rsi_loop --epochs 4 [--cold 0.25]
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import random
import time
from pathlib import Path

HOOK_MIN_CONF = 0.25
MAX_RESULTS = 6
OUT = Path(__file__).resolve().parent / "out"


def _bucket(qid: str) -> int:
    return int(hashlib.md5(qid.encode()).hexdigest()[:8], 16) % 10


def _cold(eid: str, frac: float) -> bool:
    return frac > 0 and int(hashlib.md5(("cold:" + eid).encode()).hexdigest()[:8], 16) % 1000 < frac * 1000


def _harmful(eid: str, frac: float) -> bool:
    return frac > 0 and int(hashlib.md5(("harm:" + eid).encode()).hexdigest()[:8], 16) % 1000 < frac * 1000


async def _reset(pg, sync, corpus, cold_frac: float) -> None:
    async with pg._pool.acquire() as conn:
        await conn.execute("TRUNCATE evolution_log")
    for e in corpus:
        e2 = copy.deepcopy(e)
        if _cold(e2.id, cold_frac):
            e2.confidence = 0.2
        await pg.upsert_enzyme(e2)
        await sync.sync_enzyme(e2)


def _injected(results) -> list:
    return [r for r in results if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory]


async def _evaluate(engine, queries, harm: float = 0.0) -> dict:
    exp_total = exp_hit = inj_total = inj_rel = sil_n = sil_ok = 0
    harm_exposed = n_primes = 0
    rr, lat = [], []
    for q in queries:
        t0 = time.perf_counter()
        ids = [r.enzyme.id for r in _injected(await engine.match(q["text"], max_results=MAX_RESULTS))]
        lat.append(time.perf_counter() - t0)
        n_primes += 1
        harm_exposed += any(_harmful(i, harm) for i in ids)
        good_exp = [e for e in q["expected_ids"] if not _harmful(e, harm)]
        rel = {e for e in set(q["expected_ids"]) | set(q["acceptable_ids"]) if not _harmful(e, harm)}
        if q["kind"] == "silent":
            sil_n += 1
            sil_ok += not ids
            continue
        if not good_exp:
            continue                                   # 唯一的 expected 是有害记忆:不计召回
        exp_total += len(good_exp)
        exp_hit += len(set(good_exp) & set(ids))
        inj_total += len(ids)
        inj_rel += len(set(ids) & rel)
        rank = next((i + 1 for i, x in enumerate(ids) if x in good_exp), None)
        rr.append(1 / rank if rank else 0.0)
    lat.sort()
    return {"recall": round(exp_hit / exp_total, 4), "precision": round(inj_rel / inj_total, 4) if inj_total else 1.0,
            "silence": round(sil_ok / sil_n, 4) if sil_n else None, "mrr": round(sum(rr) / len(rr), 4),
            "avg_k": round(inj_total / max(1, len(rr)), 2), "lat_p50": round(lat[len(lat) // 2], 3),
            "harm_exposure": round(harm_exposed / n_primes, 4) if harm else None}


async def _train_epoch(engine, queries, rng, harm: float = 0.0, noise: float = 0.0) -> dict:
    order = list(queries)
    rng.shuffle(order)
    good = bad = 0.0
    n_reports = 0
    for q in order:
        rel = {e for e in set(q["expected_ids"]) | set(q["acceptable_ids"]) if not _harmful(e, harm)}
        ids = [r.enzyme.id for r in _injected(await engine.match(q["text"], max_results=MAX_RESULTS))]
        if not ids:
            continue
        # agent:这批直觉有没有帮上忙——有好的相关记忆、且没被有害记忆带偏
        success = any(i in rel for i in ids) and not any(_harmful(i, harm) for i in ids)
        if noise and rng.random() < noise:
            success = not success
        await engine.report_batch(ids, success=success, detail="")
        n_reports += 1
        for rank, eid in enumerate(ids):
            credit = 1.0 if rank == 0 else 0.5 ** rank
            if credit < 0.05:
                break
            # 真值:好的相关记忆该奖、其余(无关/有害)该罚
            if success == (eid in rel):
                good += credit
            else:
                bad += credit
    return {"reports": n_reports, "credit_accuracy": round(good / (good + bad), 4) if good + bad else None}


async def _memory_stats(pg, corpus, queries, harm: float) -> dict:
    """学习是否学对了:被训练触发过的有害记忆有多少被压到 hook 门槛下(压制率),
    被触发过的好记忆有多少仍在门槛上(保留率,衡量噪声反馈的误伤)。"""
    relevant = {e for q in queries for e in set(q["expected_ids"]) | set(q["acceptable_ids"])}
    es = await pg.get_enzymes([e.id for e in corpus])
    harm_seen = [e for e in es if _harmful(e.id, harm) and e.trigger_count > 0]
    good_seen = [e for e in es if e.id in relevant and not _harmful(e.id, harm) and e.trigger_count > 0]
    return {
        "harm_seen": len(harm_seen),
        "harm_suppressed": round(sum(e.confidence < HOOK_MIN_CONF for e in harm_seen) / len(harm_seen), 4)
        if harm_seen else None,
        "good_seen": len(good_seen),
        "good_retained": round(sum(e.confidence >= HOOK_MIN_CONF for e in good_seen) / len(good_seen), 4)
        if good_seen else None,
    }


async def _cold_recovered(pg, corpus, cold_frac: float) -> dict | None:
    cold_ids = [e.id for e in corpus if _cold(e.id, cold_frac)]
    if not cold_ids:
        return None
    es = await pg.get_enzymes(cold_ids)
    return {"cold": len(cold_ids), "above_hook": sum(e.confidence >= HOOK_MIN_CONF for e in es)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--cold", type=float, default=0.0)
    ap.add_argument("--arms", default="base,jev,jev_nocredit")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--harmful", type=float, default=0.0)
    ap.add_argument("--noise", type=float, default=0.0)
    ap.add_argument("--eval-train", action="store_true", help="每轮也在 train 上评估(熟悉情境再现)")
    args = ap.parse_args()

    from app.core.engine import Engine
    from app.storage.sync import SyncManager
    from tests.eval.bootstrap import eval_stores
    from tests.eval.validate import CORPUS_DIR, QUERIES_PATH, load_corpus, load_queries

    config, pg, qdrant = await eval_stores()
    engine = Engine(config, pg, qdrant)
    corpus = load_corpus(CORPUS_DIR)
    queries = load_queries(QUERIES_PATH, {e.id for e in corpus})
    train = [q for q in queries if _bucket(q["id"]) < 7]
    test = [q for q in queries if _bucket(q["id"]) >= 7]
    print(f"train={len(train)} test={len(test)} cold={args.cold}", flush=True)

    results = {}
    for arm in args.arms.split(","):
        # base=原剂量;jev=Stage 4.5 + 适用性感知学分;jev_nocredit=只开 Stage 4.5(消融);
        # *_rw=学习规则换成 Rescorla-Wagner 预测误差(CE_HEBBIAN_RULE=rw)
        os.environ["CE_JEV_MODE"] = "off" if arm.startswith("base") else "on"
        os.environ["CE_JEV_CREDIT"] = "off" if arm == "jev_nocredit" else "on"
        os.environ["CE_HEBBIAN_RULE"] = "rw" if arm.endswith("_rw") else "layered"
        await _reset(pg, SyncManager(pg, qdrant), corpus, args.cold)
        random.seed(args.seed)                      # 探索槽(pipeline 用全局 random)
        rng = random.Random(args.seed)
        hist = [{"epoch": 0, "test": await _evaluate(engine, test, args.harmful),
                 "cold": await _cold_recovered(pg, corpus, args.cold)}]
        if args.eval_train:
            hist[0]["train"] = await _evaluate(engine, train, args.harmful)
        print(arm, hist[-1], flush=True)
        for ep in range(1, args.epochs + 1):
            tr = await _train_epoch(engine, train, rng, args.harmful, args.noise)
            row = {"epoch": ep, "train_feedback": tr, "test": await _evaluate(engine, test, args.harmful),
                   "cold": await _cold_recovered(pg, corpus, args.cold),
                   "memory": await _memory_stats(pg, corpus, queries, args.harmful)}
            if args.eval_train:
                row["train"] = await _evaluate(engine, train, args.harmful)
            hist.append(row)
            print(arm, hist[-1], flush=True)
        results[arm] = hist

    OUT.mkdir(exist_ok=True)
    tag = f"rsi_e{args.epochs}_cold{args.cold}_harm{args.harmful}_noise{args.noise}"
    (OUT / f"{tag}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
