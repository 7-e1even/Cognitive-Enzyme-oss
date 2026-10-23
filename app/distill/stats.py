"""判定记录概况:攒了多少判定、多少带真实结果、各位老师标了多少、占多大空间。

  python -m app.distill.stats            # 读 CE_PG_* 指向的库
"""

from __future__ import annotations

import asyncio
import json


async def collect(pg) -> dict:
    async with pg._pool.acquire() as c:
        total = await c.fetchval("SELECT count(*) FROM decision_log")
        by_source = await c.fetch("SELECT coalesce(policy->>'source', 'prime') AS k, count(*) AS n "
                                  "FROM decision_log GROUP BY 1 ORDER BY 2 DESC")
        by_judge = await c.fetch("SELECT coalesce(policy->>'model', policy->>'judge', '') AS k, count(*) AS n "
                                 "FROM decision_log GROUP BY 1 ORDER BY 2 DESC")
        last_day = await c.fetchval("SELECT count(*) FROM decision_log WHERE created_at > NOW() - interval '1 day'")
        outcomes = await c.fetch("SELECT outcome AS k, count(*) AS n FROM decision_outcomes GROUP BY 1 ORDER BY 2 DESC")
        linked = await c.fetchval("SELECT count(DISTINCT decision_id) FROM decision_outcomes WHERE decision_id <> ''")
        teachers = await c.fetch("SELECT teacher AS k, count(*) AS n FROM teacher_labels GROUP BY 1 ORDER BY 2 DESC")
        size = await c.fetchval("SELECT pg_total_relation_size('decision_log') + pg_total_relation_size('decision_outcomes')"
                                " + pg_total_relation_size('teacher_labels')")
    return {"decisions": total, "last_24h": last_day, "by_source": {r["k"]: r["n"] for r in by_source},
            "by_judge": {r["k"]: r["n"] for r in by_judge}, "outcomes": {r["k"]: r["n"] for r in outcomes},
            "decisions_with_direct_outcomes": linked, "teacher_labels": {r["k"]: r["n"] for r in teachers},
            "storage_mb": round((size or 0) / 1e6, 1)}


async def _main() -> dict:
    from app.config import Config
    from app.storage.pg_store import PGStore
    pg = PGStore(Config.from_env().pg)
    await pg.connect()
    try:
        return await collect(pg)
    finally:
        await pg.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), ensure_ascii=False, indent=1))
