"""方法论种子库:把专家 / 天才的思考方法文档导入成记忆,给 rsi.py 的 seed、seed_self 组当起点。

走产品的文档导入(app/learning/ingest.py:切块 → 提炼 → 撞库 → 质量门);每块占一个全局槽位,
不和正在跑的经验流抢 GLM 并发。建好的库 ce_rsi_seed_<name> 作为模板,各组开跑时复制一份再重建向量索引。
导入结果(每条记忆的正文、标签、来源)另存 data/rsi/seed_<name>.memories.json 备查。

  .venv-eval/bin/python -m tests.eval.hardbench.seed --name genius --file "天才是怎样思考？.docx:天才是怎样思考"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from tests.eval.hardbench.rsi import DATA, OUT, make_engine
from tests.eval.realbench.uplift_eval import SLOTS

_IDEA_HEAD = re.compile(r"(?im)^\s*(idea|fact)\s*(\d+)\s*[:.]")
_BLOCK_END = re.compile(r"(?im)^\s*(?:pr\s*\d+\.|idea\s*\d+|fact\s*\d+|\d+(?:\.\d+)*\s+[A-Z][^\n]{0,60}$)")


def kalda_ideas(pdf: Path, title: str) -> str:
    """Jaan Kalda 讲义里标出的 idea / fact 方法框(双栏排版:逐页左右两栏分别抽文字,再按顺序拼)。
    只取方法框本身,不取习题与解答。"""
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    w, h = (float(x) for x in re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", info).groups())
    text = []
    for pg in range(1, pages + 1):
        for x in (0, w / 2):
            text.append(subprocess.run(
                ["pdftotext", "-f", str(pg), "-l", str(pg), "-x", str(int(x)), "-y", "0", "-W", str(int(w / 2) + 1),
                 "-H", str(int(h) + 1), str(pdf), "-"], capture_output=True, text=True).stdout)
    full = "\n".join(text)
    out, seen = [], set()
    for m in _IDEA_HEAD.finditer(full):
        # 只要 idea(解题思路);fact 多是牛顿定律、基尔霍夫定律这类模型本来就会的基础事实。
        # 同一编号只取第一次出现——讲义里正式给出的那一处;后面解答里"用 idea 52"之类的引用不算
        if m.group(1).lower() != "idea" or m.group(2) in seen:
            continue
        rest = full[m.end():m.end() + 1500]
        stop = _BLOCK_END.search(rest)
        block = rest[:stop.start()] if stop else rest
        block = re.split(r"\n\s*\n", block.strip())[0]
        block = re.sub(r"\s+", " ", block).strip()
        if len(block) >= 20:
            seen.add(m.group(2))
            out.append(f"Idea {m.group(2)}: {block}")
    return f"Jaan Kalda — {title}(物理竞赛讲义中的解题思路)\n\n" + "\n\n".join(out)


def _grams8(text: str) -> set[str]:
    w = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(w[i:i + 8]) for i in range(len(w) - 7)}


async def drop_leaks(engine) -> list[dict]:
    """种子记忆与测试题的防泄漏:与任何一道 hardbench 题连续 8 个词相同的记忆删掉。"""
    grams = set()
    for p in json.loads((DATA / "problems.json").read_text(encoding="utf-8")):
        grams |= _grams8(p["problem"])
    dropped = []
    for e in await engine.pg.list_enzymes(limit=100000):
        g = _grams8(f"{e.then} {e.because}")
        if g & grams:
            dropped.append({"id": e.id, "then": e.then[:200]})
            await engine.delete_enzyme(e.id)
    return dropped


async def build(name: str, files: list[tuple[Path, str]], source_db: str, base: str = "",
                kalda: list[tuple[Path, str]] | None = None) -> dict:
    from app.learning.ingest import _MIN_SALIENCE, chunks, extract_from_chunk, read_text, store

    engine, config = await make_engine("seed", name, source_db, fresh=True,
                                       seed_db=f"ce_rsi_seed_{base}" if base else "")
    stats: dict = defaultdict(int)
    docs = [(read_text(path), source) for path, source in files]
    docs += [(kalda_ideas(path, title), f"Kalda·{title}") for path, title in (kalda or [])]
    for text, source in docs:
        parts = chunks(text)
        for k, chunk in enumerate(parts, 1):
            async with SLOTS.hold():
                for cand in await extract_from_chunk(chunk, source):
                    stats["extracted"] += 1
                    stats["scope_general" if "scope:general" in cand["tags"] else "scope_specific"] += 1
                    if cand["salience"] < _MIN_SALIENCE:
                        stats["low_salience"] += 1
                        continue
                    stats[await store(engine, cand)] += 1
            print(f"{source} {k}/{len(parts)} {dict(stats)}", flush=True)
    await engine.drain(timeout=60)
    dropped = await drop_leaks(engine)
    memories = [{"id": e.id, "then": e.then, "because": e.because, "tags": e.tags, "domain": e.domain,
                 "status": e.status, "confidence": round(e.confidence, 2)}
                for e in await engine.pg.list_enzymes(limit=100000)]
    (OUT / f"seed_{name}.memories.json").write_text(json.dumps(memories, ensure_ascii=False, indent=1), encoding="utf-8")
    await engine.pg.close()
    return {"db": config.pg.database, "memories": len(memories), "dropped_leaks": len(dropped), **stats}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--file", action="append", default=[], help="路径:来源名(.txt / .md / .docx),可重复")
    ap.add_argument("--kalda", action="append", default=[], help="Kalda 讲义 PDF 路径:标题,只取 idea / fact 方法框")
    ap.add_argument("--base", default="", help="在已建好的种子库 ce_rsi_seed_<base> 之上继续导入")
    ap.add_argument("--source-db", default="cognitive_enzyme_test")
    args = ap.parse_args()
    split = lambda f: (Path(f.rsplit(":", 1)[0]), f.rsplit(":", 1)[1])     # noqa: E731
    logging.basicConfig(level=logging.WARNING)
    print(json.dumps(asyncio.run(build(args.name, [split(f) for f in args.file], args.source_db, args.base,
                                       [split(f) for f in args.kalda])), ensure_ascii=False))


if __name__ == "__main__":
    main()
