"""文档导入:把一篇文档(方法论、手册、讲义……)提炼成记忆。

流程与 capture 一致,只是输入换成文档片段:
  切块(按段落聚到约 2500 字)→ 每块提炼候选(通用方法论 / 领域方法,写清适用条件和具体做法)
  → 撞库:没撞上走 harvest(质量门、领域归类),撞上了由 LLM 裁决新增 / 补充 / 强化 / 跳过。
候选打上 source:<来源> 标签;通用方法论另打 scope:general。宁缺毋滥:口号、故事、练习题本身不提炼。

  python -m app.learning.ingest --file 天才是怎样思考？.docx --source 天才是怎样思考
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import zipfile
from pathlib import Path

from app.core.engine import Engine
from app.learning.capture import _apply, _find_similar, reconcile
from app.llm_client import chat_json

logger = logging.getLogger(__name__)

_CHUNK_CHARS = 2500
_MIN_SALIENCE = 0.55
_MAX_PER_CHUNK = 6
# 全局刹车:每块 1 次提炼 + 每候选最多 6 次(撞库向量 + reconcile)LLM 调用,
# 大文档不加顶会扇出到上千次调用
_MAX_CHUNKS = 200


def read_text(path: Path) -> str:
    """.txt / .md 直接读;.docx 取正文段落(不依赖 python-docx)。"""
    if path.suffix.lower() == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        paras = []
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
            text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
            paras.append(text)
        import html
        return html.unescape(html.unescape("\n".join(paras)))      # 有的文档实体被转义了两次(&amp;#8226;)
    return path.read_text(encoding="utf-8", errors="replace")


def chunks(text: str, size: int = _CHUNK_CHARS) -> list[str]:
    out, cur = [], ""
    for para in (p.strip() for p in re.split(r"\n\s*\n|\n", text)):
        if not para:
            continue
        if cur and len(cur) + len(para) > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


async def extract_from_chunk(chunk: str, source: str) -> list[dict]:
    prompt = f"""下面是文档《{source}》的一个片段。请从中提取**能直接用来解决问题**的方法,做成可复用的「直觉规则」候选:
在什么情况下(trigger)、具体怎么做(then)、为什么有效(because)。

只提取:
- 通用方法论:面对问题时怎么拆解、怎么抓本质、怎么产生和筛选思路、怎么换角度、怎么排查与验证、卡住了怎么办——换一个领域也用得上
- 领域方法:某类问题真正起作用的方法、关键想法、有用的结论(写清适用条件)

**排除**:没有具体做法的口号(只说"要坚持""要自信"而不说怎么做)、故事与轶事本身、练习题的题目与答案、目录与广告。
每条都必须能落到"在什么情况下、具体做什么";宁缺毋滥。

片段:
\"\"\"
{chunk}
\"\"\"

返回 JSON 数组,每个元素:
{{"trigger": "适用情境的一句话描述", "then": "具体怎么做", "because": "为什么有效", "tags": ["可选标签"], "salience": 0.0到1.0的实用价值, "scope": "general(通用方法论)或 specific"}}
没有值得提炼的就返回 []。"""
    result = await chat_json(system="你负责把文档里的方法沉淀成可复用的行动规则。只返回 JSON 数组。", user=prompt)
    out = []
    for item in result if isinstance(result, list) else []:
        if not isinstance(item, dict) or not (item.get("then") or "").strip():
            continue
        tags = [t for t in (item.get("tags") or []) if isinstance(t, str)] + [f"source:{source}"]
        if str(item.get("scope") or "").strip().lower() == "general":
            tags.append("scope:general")
        out.append({"trigger": (item.get("trigger") or "").strip(), "then": item["then"].strip(),
                    "because": (item.get("because") or "").strip(), "domain": "", "tags": tags,
                    "salience": float(item.get("salience", 0.5) or 0.0)})
    return out[:_MAX_PER_CHUNK]


async def store(engine: Engine, cand: dict) -> str:
    """与 capture 相同的入库路由,返回处置结果。"""
    hits = await _find_similar(engine, cand)
    if not hits:
        res = await engine.harvest(insight=cand["then"], context=cand["trigger"], domain=cand.get("domain", ""),
                                   tags=cand["tags"])
        return f"add:{res.get('decision') or res.get('status')}"
    verdict = await reconcile(cand, hits)
    if not verdict:
        return "reconcile_failed"
    summary: dict = {"added": 0, "updated": 0, "reinforced": 0, "invalidated": 0, "skipped": 0,
                     "known": 0, "errors": 0}
    await _apply(engine, cand, verdict, summary, [], session_id="")
    return (verdict.get("action") or "SKIP").lower()


async def ingest_document(engine: Engine, text: str, source: str) -> dict:
    stats: dict = {"chunks": 0, "extracted": 0, "low_salience": 0}
    all_chunks = chunks(text)
    if len(all_chunks) > _MAX_CHUNKS:
        logger.warning("ingest %s: 文档切出 %d 块,超过上限 %d,只处理前 %d 块",
                       source, len(all_chunks), _MAX_CHUNKS, _MAX_CHUNKS)
        all_chunks = all_chunks[:_MAX_CHUNKS]
        stats["truncated"] = len(all_chunks)
    for chunk in all_chunks:
        stats["chunks"] += 1
        try:
            cands = await extract_from_chunk(chunk, source)
        except Exception:
            logger.exception("ingest extract failed (chunk %d)", stats["chunks"])
            continue
        for cand in cands:
            stats["extracted"] += 1
            if cand["salience"] < _MIN_SALIENCE:
                stats["low_salience"] += 1
                continue
            try:
                decision = await store(engine, cand)
            except Exception:
                logger.exception("ingest store failed")
                decision = "error"
            stats[decision] = stats.get(decision, 0) + 1
        logger.info("ingest %s chunk %d: %s", source, stats["chunks"], stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--source", required=True, help="来源名,记为 source:<来源> 标签")
    args = ap.parse_args()

    async def _go() -> dict:
        from app.config import Config
        from app.runtime import cli_admin_state
        from app.storage.pg_store import PGStore
        from app.storage.qdrant_store import QdrantStore
        await cli_admin_state()
        config = Config.from_env()
        from app import model_registry, settings
        config = model_registry.apply_roles(config, model_registry.current(),
                                            {k: settings.get(k) for k in settings.SPECS if k.startswith("model.")})
        pg = PGStore(config.pg)
        await pg.connect()
        qdrant = QdrantStore(config.qdrant, config.embedding)
        await qdrant.connect()
        try:
            return await ingest_document(Engine(config, pg, qdrant), read_text(Path(args.file)), args.source)
        finally:
            await pg.close()

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(asyncio.run(_go()), ensure_ascii=False))


if __name__ == "__main__":
    main()
