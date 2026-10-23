"""老师批量标注:把还没被某位老师标过的判定记录原样交给它作答,存进 teacher_labels。

这是"慢思考的 System 2 复核快思考的 System 1"的离线一步:线上判定由当前判官做出(on-policy
状态),老师事后给每道题完整的概率分布,供训练自己的判官使用。不在热路径上,可以用最慢
但最准的模型。

  python -m app.distill.label --teacher glm-5.3 --limit 200 [--since 2026-09-01]

老师取自后台「模型」页(按注册名);旧的 CE_TEACHERS_FILE / key_env 写法仍然有效。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def label(teacher_name: str, limit: int = 200, since: datetime | None = None, pg=None) -> dict:
    from app.distill.teachers import load_teachers
    own = pg is None
    if own:
        from app import runtime
        from app.config import Config
        from app.storage.pg_store import PGStore
        pg = PGStore(Config.from_env().pg)
        await pg.connect()
        await runtime.load_admin_state(pg)      # 老师用后台「模型」页的配置
    teacher = load_teachers([teacher_name])[teacher_name]
    stats = {"teacher": teacher_name, "picked": 0, "ok": 0, "failed": 0, "tokens": 0}
    try:
        decisions = await pg.fetch_decisions(since=since, limit=limit, unlabeled_by=teacher_name)
        stats["picked"] = len(decisions)
        sem = asyncio.Semaphore(teacher.spec.concurrency)

        async def one(d):
            req = d.get("request") or {}
            async with sem:
                ans = await teacher.answer(req.get("state", ""), req.get("questions") or {})
            if ans.answers is None:
                stats["failed"] += 1
                logger.warning("teacher %s failed on %s: %s", teacher_name, d["id"], ans.error[:200])
                return
            await pg.upsert_teacher_label(d["id"], teacher_name, ans.answers, ans.usage)
            stats["ok"] += 1
            stats["tokens"] += int(ans.usage.get("total_tokens") or 0)

        await asyncio.gather(*(one(d) for d in decisions))
    finally:
        if own:
            await pg.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teacher", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--since", default="")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    stats = asyncio.run(label(args.teacher, args.limit, datetime.fromisoformat(args.since) if args.since else None))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
