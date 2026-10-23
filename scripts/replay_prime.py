#!/usr/bin/env python3
"""在 RSI 实验库的一次性副本上重放 prime,量判官成功率与注入条数(不解题、不调 GLM)。

用法:  .venv-eval/bin/python scripts/replay_prime.py [--arm self_v2] [--n 40] [--break-jev]
--break-jev:让 Jev 判定每次都立即失败(模拟 422),看大模型接手的效果。
副本库 ce_rsi_replay 每次重建(从 ce_rsi_phys_<arm> 复制);向量集合直接读 rsi_phys_<arm>(只读)。
"""
import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOOK_MIN_CONF, HOOK_MAX = 0.25, 6           # 与 tests/eval/hardbench/rsi.py 一致
REPLAY_DB = "ce_rsi_replay"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="self_v2")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--source-db", default="cognitive_enzyme_test")
    ap.add_argument("--break-jev", action="store_true")
    args = ap.parse_args()

    import asyncpg
    from dotenv import load_dotenv

    import app.matching.pipeline as pipeline
    from app import model_registry, settings
    from app.config import Config
    from app.core.engine import Engine
    from app.llm_client import set_default_configs
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
    exp_db, coll = f"ce_rsi_phys_{args.arm}", f"rsi_phys_{args.arm}"

    conn = await asyncpg.connect(dsn=base.pg.dsn, timeout=10)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{REPLAY_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{REPLAY_DB}" TEMPLATE "{exp_db}"')
    finally:
        await conn.close()
    config = replace(config, pg=replace(config.pg, database=REPLAY_DB),
                     qdrant=replace(config.qdrant, collection_name=coll))
    set_default_configs(config.llm, config.embedding)
    pg = PGStore(config.pg)
    await pg.connect()
    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    pipeline._EXPLORATION_RATE = 0.0
    if args.break_jev:
        async def broken_jev(*a, **k):
            return None
        pipeline.judge_applicability = broken_jev
    engine = Engine(config, pg, qdrant)

    data = ROOT / "tests/eval/hardbench/data"
    order = json.loads((data / "rsi/phys.meta.json").read_text(encoding="utf-8"))["order"]
    problems = {p["id"]: p for p in json.loads((data / "problems.json").read_text(encoding="utf-8"))}
    step = max(1, len(order) // args.n)
    sample = [problems[i] for i in order[::step][:args.n]]

    jl = config.judge_llm or config.llm
    print(f"判官 {config.typesafe.model}{'(强制失效)' if args.break_jev else ''} · 接手 {jl.model} {jl.extra or ''} · "
          f"记忆库 {exp_db}(副本) · 题 {len(sample)} · "
          f"judge.token_budget={settings.get('judge.token_budget') if 'judge.token_budget' in settings.SPECS else '(无此设置)'} · "
          f"judge.fallback_budget={settings.get('judge.fallback_budget') if 'judge.fallback_budget' in settings.SPECS else '(无此设置)'}")
    status, counts, reasons, secs, llm = Counter(), Counter(), Counter(), [], Counter()
    for p in sample:
        trace: dict = {}
        t = time.perf_counter()
        try:
            results = await engine.match(p["problem"], max_results=HOOK_MAX, source="prime", trace=trace)
        except Exception as e:
            status[f"error:{type(e).__name__}"] += 1
            continue
        secs.append(time.perf_counter() - t)
        picked = [r for r in results if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory][:HOOK_MAX]
        status[trace.get("jev_status", "?")] += 1
        llm[trace.get("llm_judge_status", "-")] += 1
        counts[len(picked)] += 1
        reasons.update(r.reason or "?" for r in picked)
    await pg.close()

    n = sum(status.values())
    print("判官状态: " + " · ".join(f"{k} {v}/{n}" for k, v in status.most_common()))
    print("大模型判官: " + " · ".join(f"{k} {v}/{n}" for k, v in llm.most_common()))
    print("注入条数: " + " ".join(f"{k}条×{counts[k]}" for k in sorted(counts)))
    print("入选路径: " + " · ".join(f"{k} {v}" for k, v in reasons.most_common()))
    if secs:
        secs.sort()
        print(f"prime 耗时: 中位 {secs[len(secs) // 2]:.1f}s · 最慢 {secs[-1]:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
