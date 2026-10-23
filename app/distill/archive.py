"""判定数据归档:定期把新增的判定记录 / 事后结果 / 老师标注追加导出成压缩 JSONL,长期保存供训练。

- 目录:运行参数 distill.archive_dir(默认 data/distill/archive;留空关闭),每类数据按天一个文件:
  decision_log-YYYY-MM-DD.jsonl.gz、decision_outcomes-….jsonl.gz、teacher_labels-….jsonl.gz(追加写,
  多次写入的 gzip 分段拼接仍是合法的 gzip);进度水位记在 <目录>/state.json。
- 数据库只清理"已归档且超过保留期(distill.keep_days,默认 90 天)"的记录;归档关闭时永不清理——
  训练数据宁可多占空间也不丢。
- 每 distill.archive_hours(默认 6)小时跑一次;也可在后台「判定记录」页或 python -m app.distill.archive 手动触发
- 导出时 python -m app.distill.export --archive <目录> 读归档(可与库里未归档的部分合并)。
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import settings
from app.distill.records import keep_days

logger = logging.getLogger(__name__)

TABLES = ("decision_log", "decision_outcomes", "teacher_labels")
_JSON_COLS = {"policy", "request", "answers", "candidates", "injected", "usage"}
_SETTLE = timedelta(minutes=1)     # 只归档一分钟前的行:刚提交的写入不会被漏掉


def archive_dir() -> Path | None:
    raw = (settings.get("distill.archive_dir") or "").strip()
    return None if raw.lower() in ("", "off", "none") else Path(raw)


def _load_state(d: Path) -> dict:
    f = d / "state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _row(r) -> dict:
    out = {}
    for k, v in dict(r).items():
        if k in _JSON_COLS and isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                pass
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


async def archive_once(pg, out: Path) -> dict:
    """把水位之后的新行追加进归档(游标流式读取,不分页,同一时间戳的行不会漏),
    再清理已归档且过期的行。返回各表归档条数。"""
    out.mkdir(parents=True, exist_ok=True)
    state = _load_state(out)
    stats = {}
    now = datetime.now(timezone.utc)
    until = now - _SETTLE
    day = now.strftime("%Y-%m-%d")
    async with pg._pool.acquire() as c:
        for table in TABLES:
            mark = datetime.fromisoformat(state[table]) if state.get(table) else datetime(1970, 1, 1, tzinfo=timezone.utc)
            n, last = 0, None
            with gzip.open(out / f"{table}-{day}.jsonl.gz", "at", encoding="utf-8") as f:
                async with c.transaction():
                    async for r in c.cursor(f"SELECT * FROM {table} WHERE created_at > $1 AND created_at <= $2"
                                            f" ORDER BY created_at", mark, until):
                        f.write(json.dumps(_row(r), ensure_ascii=False, default=str) + "\n")
                        n, last = n + 1, r["created_at"]
            if last is not None:
                state[table] = last.isoformat()
            stats[table] = n
        # 清理:只删"已归档(不晚于水位)且超过保留期"的行
        cutoff = now - timedelta(days=keep_days())
        if state.get("decision_log"):
            wl = min(cutoff, datetime.fromisoformat(state["decision_log"]))
            await c.execute("DELETE FROM teacher_labels WHERE decision_id IN "
                            "(SELECT id FROM decision_log WHERE created_at <= $1)", wl)
            await c.execute("DELETE FROM decision_log WHERE created_at <= $1", wl)
        if state.get("decision_outcomes"):
            await c.execute("DELETE FROM decision_outcomes WHERE created_at <= $1",
                            min(cutoff, datetime.fromisoformat(state["decision_outcomes"])))
    (out / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


async def archive_loop(engine) -> None:
    """后台循环:启动 5 分钟后跑第一次,之后每 distill.archive_hours 小时一次。"""
    await asyncio.sleep(300)
    while True:
        d = archive_dir()
        if d is not None:
            try:
                stats = await archive_once(engine.pg, d)
                if any(stats.values()):
                    logger.info("decision archive: %s → %s", stats, d)
            except Exception:
                logger.exception("decision archive failed (will retry next round)")
        await asyncio.sleep(max(0.1, settings.get("distill.archive_hours")) * 3600)


def read_archive(d: Path) -> dict[str, list[dict]]:
    """读回归档:{表名: 行列表}(按主键去重,后写覆盖先写)。"""
    out: dict[str, list[dict]] = {}
    for table in TABLES:
        seen: dict = {}
        for f in sorted(d.glob(f"{table}-*.jsonl.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    key = r.get("id") if table != "teacher_labels" else (r.get("decision_id"), r.get("teacher"))
                    seen[key] = r
        out[table] = list(seen.values())
    return out


async def _main() -> dict:
    from app import settings
    from app.config import Config
    from app.storage.pg_store import PGStore
    pg = PGStore(Config.from_env().pg)
    await pg.connect()
    try:
        await settings.load(pg)          # 归档目录与保留天数以后台配置为准
        d = archive_dir()
        if d is None:
            return {"archive": "off"}
        return {"archive": str(d), **(await archive_once(pg, d))}
    finally:
        await pg.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_main()), ensure_ascii=False))
