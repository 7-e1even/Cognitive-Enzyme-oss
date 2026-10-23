#!/usr/bin/env python3
"""标定"Jev 失效时由 LLM(GLM)接手判定":同一批题、同一个判定池,Jev 与 LLM 各判一次,
以 Jev 的最终注入为参照,看 LLM 按"概率 ≥ τ、至多 k 条"挑选时与 Jev 的一致程度,以及 LLM 的耗时。

用法:  .venv-eval/bin/python scripts/calib_llm_judge.py [--arm self_v2] [--n 60] [--model glm-5.3]
在 ce_rsi_replay(实验库副本)上跑,原实验库不动;结果逐题写到 /tmp/calib_llm_judge.jsonl。
"""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
REPLAY_DB = "ce_rsi_replay"
OUT = Path("/tmp/calib_llm_judge.jsonl")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="self_v2")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--model", default="glm-5.3")
    ap.add_argument("--source-db", default="cognitive_enzyme_test")
    args = ap.parse_args()

    import asyncpg
    from dotenv import load_dotenv

    import app.matching.pipeline as pipeline
    from app import model_registry, settings
    from app.config import Config
    from app.core.engine import Engine
    from app.llm_client import set_default_configs
    from app.matching.llm_judge import judge_llm
    from app.storage.pg_store import PGStore
    from app.storage.qdrant_store import QdrantStore

    load_dotenv(ROOT / ".env")
    base = Config.from_env()
    src = PGStore(replace(base.pg, database=args.source_db))
    await src.connect()
    try:
        reg = await model_registry.fetch(src)
        values = await settings.read(src, [k for k in settings.SPECS if k.startswith("model.")])
    finally:
        await src.close()
    config = model_registry.apply_roles(base, reg, values)
    judge_cfg = reg.llm_config(args.model) or config.llm
    judge_cfg = replace(judge_cfg, extra={**(judge_cfg.extra or {}), "thinking": {"type": "disabled"}})

    conn = await asyncpg.connect(dsn=base.pg.dsn, timeout=10)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{REPLAY_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{REPLAY_DB}" TEMPLATE "ce_rsi_phys_{args.arm}"')
    finally:
        await conn.close()
    config = replace(config, pg=replace(config.pg, database=REPLAY_DB),
                     qdrant=replace(config.qdrant, collection_name=f"rsi_phys_{args.arm}"))
    set_default_configs(config.llm, config.embedding)
    pg = PGStore(config.pg)
    await pg.connect()
    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    pipeline._EXPLORATION_RATE = 0.0
    engine = Engine(config, pg, qdrant)

    data = ROOT / "tests/eval/hardbench/data"
    order = json.loads((data / "rsi/phys.meta.json").read_text(encoding="utf-8"))["order"]
    problems = {p["id"]: p for p in json.loads((data / "problems.json").read_text(encoding="utf-8"))}
    step = max(1, len(order) // args.n)
    sample = [problems[i] for i in order[1::step][:args.n]]          # 与 replay_prime.py 错开一位,换一批题
    print(f"Jev {config.typesafe.model} vs LLM {judge_cfg.model}(关思考) · 题 {len(sample)}", flush=True)

    OUT.write_text("", encoding="utf-8")
    for k, p in enumerate(sample):
        trace: dict = {}
        await engine.match(p["problem"], max_results=6, source="prime", trace=trace)
        if trace.get("jev_status") != "ok":
            print(f"  [{k}] Jev {trace.get('jev_status')},跳过", flush=True)
            continue
        pool_ids = [eid for eid, _ in trace["jev_pool"]]
        pool = [e for e in [await pg.get_enzyme(i) for i in pool_ids] if e is not None]
        t = time.perf_counter()
        probs = await judge_llm(p["problem"], pool, judge_cfg, timeout=60.0)
        secs = time.perf_counter() - t
        row = {"id": p["id"], "pool": pool_ids, "jev_selected": trace.get("dosed_ids", []),
               "jev_p": {eid: pj for eid, pj, _ in trace["jev"]}, "llm_p": probs, "llm_secs": round(secs, 2)}
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  [{k}] 池 {len(pool)} · Jev 选 {len(row['jev_selected'])} · LLM {'失败' if probs is None else 'ok'} {secs:.1f}s",
              flush=True)
    await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
