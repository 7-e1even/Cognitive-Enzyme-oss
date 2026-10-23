"""已会过滤回填 — 对存量记忆跑闭卷判定。

三种模式(互斥):
  默认(dry-run,只读)   逐条判 status=="active" 的酶,报告写 JSONL,不动库。
  --apply REPORT.jsonl 把报告里 covered==true 的酶隔离:status=quarantine + known:closed_book 标签。
  --rollback           把带 known:closed_book 标签的隔离酶恢复 active、摘掉标签。

  .venv-eval/bin/python scripts/known_backfill.py [--limit N] [--concurrency 6] [--out PATH]
  .venv-eval/bin/python scripts/known_backfill.py --apply logs/known_backfill-xxx.jsonl
  .venv-eval/bin/python scripts/known_backfill.py --rollback
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv

from app.core import EvolutionEvent, EventType
from app.learning import known_filter

logger = logging.getLogger(__name__)

TAG = "known:closed_book"
FAIL_REASONS = {"no-trigger", "no-answer", "error", "bad-verdict"}


async def scan(engine, limit: int | None = None, concurrency: int = 6,
               out_path: str | None = None) -> list[dict]:
    """dry-run:对全部 active 酶逐条闭卷判定,返回报告行(不写库);out_path 非空时边判边写。"""
    enzymes = await engine.pg.list_enzymes(limit=100000)
    actives = [e for e in enzymes if e.status == "active"]
    if limit:
        actives = actives[:limit]
    sem = asyncio.Semaphore(concurrency)

    async def _judge(e):
        trigger = known_filter.enzyme_trigger(e)
        covered, reason = False, "error"
        async with sem:
            for attempt in range(3):                    # 限流(429)等瞬时失败重试,退避
                covered, reason = await known_filter.judge({"trigger": trigger, "then": e.then})
                if reason not in ("no-answer", "error"):
                    break
                await asyncio.sleep(10 * (attempt + 1))
        return {"id": e.id, "origin": getattr(e.origin, "value", e.origin), "domain": e.domain or "",
                "trigger": trigger, "then": (e.then or "")[:300],
                "covered": covered, "reason": reason}

    tasks = [asyncio.ensure_future(_judge(e)) for e in actives]
    rows: list[dict] = []
    f = open(out_path, "w", encoding="utf-8") if out_path else None
    try:
        for coro in asyncio.as_completed(tasks):
            r = await coro
            rows.append(r)
            if f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
    finally:
        if f:
            f.close()
    return rows


def print_summary(rows: list[dict], report_path: str = "") -> None:
    total = len(rows)
    covered = sum(1 for r in rows if r["covered"])
    failed = sum(1 for r in rows if r["reason"] in FAIL_REASONS)
    print(f"total={total} covered={covered} not_covered={total - covered - failed} failed={failed}")
    for field in ("origin", "domain"):
        by_field = Counter(r[field] for r in rows)
        cov_field = Counter(r[field] for r in rows if r["covered"])
        parts = ", ".join(f"{k or '(空)'}:{cov_field.get(k, 0)}/{v}" for k, v in by_field.most_common())
        print(f"  by {field}: {parts}")
    if report_path:
        print(f"report: {report_path}")


async def apply_report(engine, report_path: str) -> dict:
    """按报告隔离:把 covered 的酶改 quarantine、打 known:closed_book 标签、记免疫事件。"""
    counts = {"quarantined": 0, "skipped": 0}
    for line in Path(report_path).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if not rec.get("covered"):
            continue
        e = await engine.get_enzyme(rec["id"])
        if not e or e.status != "active":
            counts["skipped"] += 1
            continue
        e.status = "quarantine"
        if TAG not in e.tags:
            e.tags = [*e.tags, TAG]
        await engine.update_enzyme(e)          # 同步 Qdrant payload,检索不再命中
        await engine.pg.log_event(EvolutionEvent(
            enzyme_id=e.id, event_type=EventType.IMMUNE,
            detail={"action": "known_quarantine", "reason": rec.get("reason", "")},
        ))
        counts["quarantined"] += 1
    return counts


async def rollback(engine) -> dict:
    """回滚:恢复所有带 known:closed_book 标签的隔离酶。"""
    counts = {"restored": 0}
    for e in await engine.pg.list_enzymes(limit=100000):
        if e.status != "quarantine" or TAG not in (e.tags or []):
            continue
        e.status = "active"
        e.tags = [t for t in e.tags if t != TAG]
        await engine.update_enzyme(e)
        await engine.pg.log_event(EvolutionEvent(
            enzyme_id=e.id, event_type=EventType.IMMUNE,
            detail={"action": "known_restore"},
        ))
        counts["restored"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", metavar="REPORT.jsonl", help="按报告隔离 covered 的酶(真实写库)")
    mode.add_argument("--rollback", action="store_true", help="恢复所有 known:closed_book 隔离酶")
    ap.add_argument("--limit", type=int, default=None, help="dry-run 只判前 N 条(冒烟用)")
    ap.add_argument("--concurrency", type=int, default=6, help="dry-run 并发数")
    ap.add_argument("--out", default=None, help="dry-run 报告路径(默认 logs/known_backfill-<时间戳>.jsonl)")
    args = ap.parse_args()

    async def _engine():
        from app.config import Config
        from app.runtime import cli_admin_state
        from app.storage.pg_store import PGStore
        from app.storage.qdrant_store import QdrantStore
        from app.core.engine import Engine
        await cli_admin_state()
        config = Config.from_env()
        from app import model_registry, settings
        config = model_registry.apply_roles(config, model_registry.current(),
                                            {k: settings.get(k) for k in settings.SPECS if k.startswith("model.")})
        from app.llm_client import set_default_configs
        set_default_configs(config.llm, config.embedding)
        pg = PGStore(config.pg)
        await pg.connect()
        qdrant = QdrantStore(config.qdrant, config.embedding)
        await qdrant.connect()
        return Engine(config, pg, qdrant)

    async def _run():
        dotenv.load_dotenv()
        engine = await _engine()
        try:
            if args.apply:
                counts = await apply_report(engine, args.apply)
                print(json.dumps(counts, ensure_ascii=False))
            elif args.rollback:
                counts = await rollback(engine)
                print(json.dumps(counts, ensure_ascii=False))
            else:
                out = args.out or f"logs/known_backfill-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                rows = await scan(engine, limit=args.limit, concurrency=args.concurrency,
                                  out_path=out)
                print_summary(rows, out)
        finally:
            await engine.pg.close()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
