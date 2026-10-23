"""记忆增量探针 — 同一道题,注入记忆 vs 裸答,看技巧有没有被救回来。

取 known_recall 报告里 active 组判过的题(裸答用过/没用上技巧都已知):
  1. engine.match(题目) —— 走和线上 prime 完全同一条召回,看目标酶能不能被检索到(Top6);
  2. 按 ce_prime_hook 的真实格式把命中酶渲染成注入块,拼在题目前让模型作答;
  3. 判官判定这份"有记忆"的解答有没有用上目标酶的技巧。

解读:裸答没用上 + 注入后用上 → 记忆产生了真实增量;检索没命中目标酶 → 瓶颈在召回。

  .venv-eval/bin/python scripts/memory_lift_probe.py --recall logs/known_recall-xxx.jsonl
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv

from app.learning import known_filter
from app.llm_client import chat, chat_json

logger = logging.getLogger(__name__)

MIN_EXEMPLAR_LEN = 400
MIN_CONFIDENCE = 0.25          # 对齐 hooks/ce_prime_hook.py
MAX_RESULTS = 6

_SOLVE_SYSTEM = "你是经验丰富的物理/工程解题者。给出求解思路和关键步骤,不用写完整计算。"

_HEADER = "[认知酶 · System 1 直觉] 以下反射被当前上下文激活,供参考(无关可忽略):"
_SEV_ICON = {"critical": "🔴", "warning": "🟡", "info": "🟢"}

_JUDGE_PROMPT = """下面是一道题、一份解答,以及一条「技巧记忆」。

题目:{problem}

技巧记忆(适用情境:{trigger};做法:{then})

解答:
\"\"\"
{answer}
\"\"\"

判断:这份解答是否实际用到了技巧记忆里的做法(用到核心一步即算,等价说法也算)?
- 解答走了完全不同的路线,或只用了通用常识 → used 为 false
- 解答用到了该技巧的关键步骤或关键结论 → used 为 true
- 拿不准时判 false

返回 JSON 对象:{{"used": true 或 false, "reason": "一句话理由"}}"""


def _render_block(matches) -> str:
    """复刻 hooks/ce_prime_hook.py 的 format_block(用精简字段)。"""
    lines = ["⟦CE:BEGIN⟧", _HEADER]
    for e in matches:
        icon = _SEV_ICON.get(e.severity.value, "⚪")
        line = f"{icon} [{e.confidence:.0%}] {e.then}"
        if e.because:
            line += f"(因为:{e.because[:150]})"
        line += f"  ‹enzyme_id={e.id}›"
        lines.append(line)
    lines.append("⟦CE:END⟧")
    return "\n".join(lines)


async def _one(engine, enzyme, problem: str, cold_used) -> dict:
    """一道题:召回 → 渲染注入块 → 带记忆作答 → 判定。"""
    trace: dict = {}
    results = await engine.match(problem[:3000], max_results=MAX_RESULTS, trace=trace, source="eval")
    injected = [r.enzyme for r in results if r.enzyme.confidence >= MIN_CONFIDENCE or r.exploratory]
    hit_target = any(e.id == enzyme.id for e in injected)
    block = _render_block(injected) if injected else ""

    answer = None
    for attempt in range(3):
        try:
            prompt = f"{block}\n\n题目:\n\n{problem}" if block else f"题目:\n\n{problem}"
            answer = await chat(system=_SOLVE_SYSTEM, user=prompt, timeout=120.0)
        except Exception:
            logger.exception("lift solve failed (non-fatal)")
        if answer:
            break
        await asyncio.sleep(10 * (attempt + 1))
    if not answer:
        return {"used": None, "reason": "no-answer", "injected": len(injected),
                "hit_target": hit_target}
    try:
        verdict = await chat_json(
            system="你是严格的评审,判断解答是否用到了某条技巧。只返回 JSON。",
            user=_JUDGE_PROMPT.format(problem=problem[:1500], then=enzyme.then,
                                      trigger=known_filter.enzyme_trigger(enzyme),
                                      answer=answer[:2500]),
        )
    except Exception:
        logger.exception("lift judge failed (non-fatal)")
        return {"used": None, "reason": "error", "injected": len(injected),
                "hit_target": hit_target}
    used = verdict.get("used") is True if isinstance(verdict, dict) else None
    return {"used": used, "reason": str(verdict.get("reason") or "") if isinstance(verdict, dict) else "bad-verdict",
            "injected": len(injected), "hit_target": hit_target,
            "injected_ids": [e.id[:8] for e in injected], "cold_used": cold_used}


async def probe(engine, recall_path: str, concurrency: int) -> list[dict]:
    recall = [json.loads(l) for l in Path(recall_path).read_text().splitlines()
              if json.loads(l)["group"] == "active" and json.loads(l)["used"] is not None]
    enzymes = {e.id: e for e in await engine.pg.list_enzymes(limit=100000)}
    sem = asyncio.Semaphore(concurrency)

    async def _run(r):
        e = enzymes.get(r["id"])
        if not e:
            return {**r, "used": None, "reason": "enzyme-gone"}
        problem = max((x for x in e.exemplars or [] if len(x) >= MIN_EXEMPLAR_LEN),
                      key=len, default=None)
        async with sem:
            res = await _one(engine, e, problem, r["used"])
        return {"id": r["id"], "domain": r["domain"], "cold_used": r["used"],
                "problem": (problem or "")[:300], "then": (e.then or "")[:300], **res}

    return await asyncio.gather(*[_run(r) for r in recall])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recall", required=True, help="known_recall_probe 的报告")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default=None)
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
            rows = await probe(engine, args.recall, args.concurrency)
            out = args.out or f"logs/memory_lift-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            rescued = sum(1 for r in rows if r.get("cold_used") is False and r.get("used") is True)
            still_cold = sum(1 for r in rows if r.get("cold_used") is False and r.get("used") is False)
            kept = sum(1 for r in rows if r.get("cold_used") is True and r.get("used") is True)
            lost = sum(1 for r in rows if r.get("cold_used") is True and r.get("used") is False)
            hits = sum(1 for r in rows if r.get("hit_target"))
            print(f"n={len(rows)} 冷启动没用→注入后用上(增量): {rescued}  仍没用上: {still_cold}")
            print(f"冷启动已会用→注入后仍用: {kept}  注入后反而没用: {lost}")
            print(f"目标酶被召回注入: {hits}/{len(rows)}")
            print(f"report: {out}")
        finally:
            await engine.pg.close()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
