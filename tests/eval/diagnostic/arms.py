"""Recall/selection arms on the frozen math-physics bank: same inputs, different pipeline settings.

Each arm runs the real ``run_pipeline`` (embedding read from cache, live rerank + Jev) on the
2026-10-16 frozen inputs and provenance filters, so arms differ only by the settings below.
Results go to ``offline/data/collection-2026-10-23/arms/<arm>.jsonl``; ``report`` is offline.

    .venv-eval/bin/python -m tests.eval.diagnostic.arms run --arm wide
    .venv-eval/bin/python -m tests.eval.diagnostic.arms report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.collect import CURRENT, FilteredVectors, FrozenPG, RecordingHTTP, configuration, index
from tests.eval.diagnostic.protocol import EVAL, append, rows, sha, verify

SOURCE = EVAL / "offline/data/collection-2026-10-16/formal"
OUT = EVAL / "offline/data/collection-2026-10-23"

# Settings that differ from the production defaults. "current" reproduces the 2026-10-16 collection.
ARMS: dict[str, dict] = {
    "current": {},
    # Judge every candidate of the nominal 36 pool: batched per-candidate scan, then one joint choice.
    "scan36": {"judge.two_stage": True, "judge.two_stage_compare": "joint", "judge.scan_pool": 36,
               "judge.compare_k": 6},
    # + score-based rerank keep, RRF pool, wider recall and a 48 pool.
    "wide": {"judge.two_stage": True, "judge.two_stage_compare": "joint", "judge.scan_pool": 48,
             "judge.pool": 48, "judge.compare_k": 6, "judge.pool_order": "rrf",
             "ranking.rerank_keep": "floor", "recall.dense_limit": 100, "recall.cap": 120},
}
HOOK_MIN_CONF, HOOK_MAX = 0.25, 6


async def run(args):
    import app.llm_client as llm
    import app.matching.pipeline as pipeline
    import app.matching.rerank as rr_module
    import app.matching.route as route
    import app.typesafe_client as tc
    from app import settings
    from app.core.features import build_search_query

    verify(SOURCE / "inputs")
    tasks = json.loads((SOURCE / "inputs/tasks.json").read_text())
    banks = json.loads((SOURCE / "inputs/banks.json").read_text())
    config, registry, metadata = await configuration()
    if args.rerank_model:   # e.g. Pro/BAAI/bge-reranker-v2-m3: same weights, paid-tier rate limits
        from dataclasses import replace
        config = replace(config, embedding=replace(config.embedding, rerank_model=args.rerank_model))
        metadata["models"]["rerank"] = args.rerank_model
    overrides = ARMS[args.arm]
    settings._values.update(overrides)
    # Batch eval hits provider limits; retry instead of silently degrading (rows still flagged below).
    settings._values.update({"ranking.rerank_retries": 6, "ranking.rerank_backoff_cap": 20.0,
                             "ranking.rerank_timeout": 60.0, "judge.timeout": 20.0, "judge.concurrency": 4})
    out = OUT / "arms"
    out.mkdir(parents=True, exist_ok=True)
    http = RecordingHTTP(OUT / "api", [p.api_key for p in registry.providers.values()])
    http.is_closed = False   # the app's lazy client getters check this attribute
    tc._client = http
    rr_module._client = http
    vectors = {r["sha256"]: r["vector"] for r in rows(OUT / "embeddings.jsonl")}

    async def cached_embed(text, config=None):
        key = sha(text[:8000])
        if key not in vectors:   # one query was never embedded in the 2026-10-16 collection
            vec = await real_embed(text, config=config)
            if vec:
                vectors[key] = vec
                append(OUT / "embeddings.jsonl", {"sha256": key, "input": text[:8000], "vector": vec})
        return vectors.get(key)
    real_embed = llm.embed
    llm.embed = cached_embed
    fingerprint = sha((SOURCE / "inputs/manifest.json").read_bytes())
    stores = await index(OUT, banks, vectors, config, fingerprint)

    path = out / f"{args.arm}.jsonl"
    done = {r["id"] for r in rows(path) if not degraded(r)}
    todo = [t for t in tasks if t["id"] not in done]
    if args.only_gold:
        todo = [t for t in todo if t["expected_ids"]]
    if args.ids:
        wanted = set(Path(args.ids).read_text().split())
        todo = [t for t in todo if t["id"] in wanted]
    if args.limit:
        todo = todo[:args.limit]
    meta = {"arm": args.arm, "overrides": overrides, "models": metadata["models"],
            "base_overrides": metadata["overrides"], "input_sha256": fingerprint}
    (out / f"{args.arm}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"arm={args.arm} todo={len(todo)} overrides={overrides}", flush=True)
    try:
        for i, task in enumerate(todo, 1):
            t0 = time.perf_counter()
            rec = {"id": task["id"], "group": task["group"], "arm": args.arm,
                   "expected_ids": task["expected_ids"], "trace": {}, "injected_ids": []}
            token = CURRENT.set(rec)
            try:
                pg = FrozenPG(banks[task["group"]], set(task["excluded"]))
                store = FilteredVectors(stores[task["group"]], set(task["excluded"]), set(pg.memories), rec)
                route._cache = None
                result = await pipeline.run_pipeline(
                    context=task["problem"]["problem"], search_query=build_search_query(task["problem"]["problem"]),
                    pg=pg, qdrant=store, max_results=HOOK_MAX, embedding_config=config.embedding,
                    typesafe_config=config.typesafe, trace=rec["trace"])
                picked = [r for r in result if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory][:HOOK_MAX]
                rec["injected_ids"] = [r.enzyme.id for r in picked]
                rec["selected"] = [{"id": r.enzyme.id, "reason": r.reason, "p": r.applicability,
                                    "confidence": r.enzyme.confidence} for r in result]
            except Exception as exc:
                rec["error"] = type(exc).__name__ + ": " + str(http.clean(str(exc)))[:400]
            finally:
                CURRENT.reset(token)
            tr = rec["trace"]
            rec["rerank_failed"] = bool(tr.get("rerank_failed"))
            rec["slim"] = {"recalled": tr.get("recalled_ids"), "reranked": tr.get("reranked_ids"),
                           "pool": [x[0] for x in tr.get("jev_pool") or []],
                           "jev": tr.get("jev"), "none": tr.get("jev_none"), "status": tr.get("jev_status"),
                           "calls": tr.get("jev_calls"), "analogy": tr.get("analogy")}
            rec.pop("dense_searches", None)
            rec["trace"] = {}
            rec["secs"] = round(time.perf_counter() - t0, 2)
            append(path, rec)
            if i % 10 == 0 or rec.get("error"):
                print(f"{args.arm} {i}/{len(todo)} {task['id']} inject={len(rec['injected_ids'])} "
                      f"status={rec['slim']['status']} secs={rec['secs']} err={rec.get('error', '')}", flush=True)
    finally:
        for s in stores.values():
            await s.close()
        await http.client.aclose()


def degraded(r: dict) -> bool:
    """Rerank or Jev failed (or some pool candidates got no score): rerun, never report as a result."""
    s = r.get("slim") or {}
    rerank = [x["status"] for x in r.get("api_refs") or [] if x["kind"] == "rerank"]
    if rerank and rerank[-1] != 200:
        return True
    unjudged = set(s.get("pool") or []) - {x[0] for x in s.get("jev") or []}
    return bool(r.get("error") or r.get("rerank_failed") or s.get("status") != "ok" or unjudged)


def latest(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in rows(path)}


def report(arms: list[str]) -> str:
    tasks = {t["id"]: t for t in json.loads((SOURCE / "inputs/tasks.json").read_text())}
    base = {r["id"]: r for r in rows(SOURCE / "retrieval.jsonl")}
    lines = ["| arm | 题 | 正例 | 召回 | 进判定池 | 注入 | 有注入的题 | 每题注入 | Jev 失败 | 每题 Jev 调用 | 秒/题 | 降级待重跑 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for arm in arms:
        data = ({k: v for k, v in base.items()} if arm == "collected" else
                latest(OUT / "arms" / f"{arm}.jsonl"))
        c = Counter()
        for pid, r in data.items():
            if r.get("error") or (arm != "collected" and degraded(r)):
                c["degraded"] += 1
                continue
            c["q"] += 1
            tr = r.get("slim") or {"recalled": r["trace"].get("refined_ids"),
                                   "pool": [x[0] for x in r["trace"].get("jev_pool") or []],
                                   "status": r["trace"].get("jev_status"), "calls": r["trace"].get("jev_calls")}
            for g in tasks[pid]["expected_ids"]:
                c["gold"] += 1
                c["recalled"] += g in (tr.get("recalled") or [])
                c["pooled"] += g in (tr.get("pool") or [])
                c["injected"] += g in r["injected_ids"]
            c["inj_q"] += bool(r["injected_ids"])
            c["inj"] += len(r["injected_ids"])
            c["jev_fail"] += tr.get("status") != "ok"
            c["calls"] += tr.get("calls") or 1
            c["secs"] += r.get("secs") or 0
        q = max(c["q"], 1)
        lines.append(f"| {arm} | {c['q']} | {c['gold']} | {c['recalled']} | {c['pooled']} | {c['injected']} | "
                     f"{c['inj_q']} | {c['inj'] / q:.2f} | {c['jev_fail']} | {c['calls'] / q:.1f} | {c['secs'] / q:.1f} | {c['degraded']} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=("run", "report"))
    ap.add_argument("--arm", choices=tuple(ARMS))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-gold", action="store_true")
    ap.add_argument("--ids", default="", help="file with question ids, one per line")
    ap.add_argument("--rerank-model", default="")
    args = ap.parse_args()
    if args.stage == "report":
        print(report(["collected"] + [a for a in ARMS if (OUT / "arms" / f"{a}.jsonl").exists()]))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
