"""自发调用探针 — 用真实题目检验「模型本来就会」是否成立。

已会过滤用的是抽象情境的闭卷测试,这里换成酶自己的真实题目经验样本:不给任何提示,
让模型直接解题,再判官判定这份裸答是否真的用上了这条记忆里的技巧。

对比两组:
  quarantine 组 — 回填时判 covered 并已隔离的记忆(预测:裸答大多仍会自发用上 → 隔离合理)
  active 组     — 回填时判 covered=false 的记忆(预测:裸答多用不上 → 留存合理)
若两组自发使用率都高,说明抽象情境的闭卷测试系统性高估"模型已会"。

  .venv-eval/bin/python scripts/known_recall_probe.py --report logs/known_backfill-xxx.jsonl \
      --apply-list logs/known_backfill-xxx.apply.jsonl [--n-quarantine 8] [--n-active 12]
"""

import argparse
import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dotenv

from app.learning import known_filter
from app.llm_client import chat, chat_json

logger = logging.getLogger(__name__)

MIN_EXEMPLAR_LEN = 400          # 低于这个长度基本是提炼出来的情境描述,不是题目原文

_SOLVE_SYSTEM = "你是经验丰富的物理/工程解题者。只凭你自己的知识,给出求解思路和关键步骤,不用写完整计算。"

_JUDGE_PROMPT = """下面是一道题、一份没有看任何提示写出的解答,以及一条「技巧记忆」。

题目:{problem}

技巧记忆(适用情境:{trigger};做法:{then})

解答:
\"\"\"
{answer}
\"\"\"

判断:这份解答是否实际用到了技巧记忆里的做法(用到核心一步即算,不要求提到记忆本身;等价做法换说法也算)?
- 解答走了完全不同的路线,或只用了通用常识 → used 为 false
- 解答用到了该技巧的关键步骤或关键结论 → used 为 true
- 拿不准时判 false

返回 JSON 对象:{{"used": true 或 false, "reason": "一句话理由"}}"""


async def _judge_problem(enzyme, problem: str, sem: asyncio.Semaphore) -> dict:
    """一道真实题目:裸答 → 判定是否自发用到该酶的技巧。失败按未用到处理。"""
    trigger = known_filter.enzyme_trigger(enzyme)
    async with sem:
        answer = None
        for attempt in range(3):                    # 限流等瞬时失败重试
            try:
                answer = await chat(system=_SOLVE_SYSTEM, user=f"题目:\n\n{problem}", timeout=120.0)
            except Exception:
                logger.exception("recall probe solve failed (non-fatal)")
            if answer:
                break
            await asyncio.sleep(10 * (attempt + 1))
        if not answer:
            return {"used": None, "reason": "no-answer"}
        try:
            verdict = await chat_json(
                system="你是严格的评审,判断解答是否用到了某条技巧。只返回 JSON。",
                user=_JUDGE_PROMPT.format(problem=problem[:1500], trigger=trigger,
                                          then=enzyme.then, answer=answer[:2500]),
            )
        except Exception:
            logger.exception("recall probe judge failed (non-fatal)")
            return {"used": None, "reason": "error"}
    if not isinstance(verdict, dict):
        return {"used": None, "reason": "bad-verdict"}
    return {"used": verdict.get("used") is True, "reason": str(verdict.get("reason") or "")}


async def probe(engine, report_path: str, apply_path: str, n_quarantine: int, n_active: int,
                concurrency: int, seed: int) -> list[dict]:
    report = {json.loads(l)["id"]: json.loads(l) for l in Path(report_path).read_text().splitlines()}
    applied = {json.loads(l)["id"] for l in Path(apply_path).read_text().splitlines()}
    enzymes = {e.id: e for e in await engine.pg.list_enzymes(limit=100000)}

    def eligible(eid: str):
        e = enzymes.get(eid)
        if not e:
            return None
        long_ex = [x for x in (e.exemplars or []) if len(x) >= MIN_EXEMPLAR_LEN]
        return max(long_ex, key=len) if long_ex else None

    q_pool = [eid for eid in applied if eligible(eid)]
    a_pool = [eid for eid, r in report.items()
              if not r["covered"] and eid not in applied and eligible(eid)]
    rng = random.Random(seed)
    rng.shuffle(q_pool); rng.shuffle(a_pool)

    picks = [("quarantine", eid) for eid in q_pool[:n_quarantine]] + \
            [("active", eid) for eid in a_pool[:n_active]]

    seen_problems: dict[str, str] = {}      # 题目原文去重(多条酶可能采自同一道题)
    sem = asyncio.Semaphore(concurrency)

    async def _one(group: str, eid: str):
        e = enzymes[eid]
        problem = eligible(eid)
        dup = seen_problems.get(problem[:200])
        seen_problems.setdefault(problem[:200], eid)
        res = await _judge_problem(e, problem, sem)
        return {"id": eid, "group": group, "domain": e.domain or "",
                "problem": problem[:300], "trigger": known_filter.enzyme_trigger(e),
                "then": (e.then or "")[:300],
                "dup_of": dup if dup != eid else None, **res}

    return await asyncio.gather(*[_one(g, eid) for g, eid in picks])


def print_summary(rows: list[dict], report_path: str = "") -> None:
    for group in ("quarantine", "active"):
        g = [r for r in rows if r["group"] == group]
        used = sum(1 for r in g if r["used"] is True)
        failed = sum(1 for r in g if r["used"] is None)
        print(f"{group}: n={len(g)} used={used} unused={len(g)-used-failed} failed={failed}")
    if report_path:
        print(f"report: {report_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="known_backfill 的 dry-run 报告")
    ap.add_argument("--apply-list", required=True, help="实际隔离清单(apply.jsonl)")
    ap.add_argument("--n-quarantine", type=int, default=8)
    ap.add_argument("--n-active", type=int, default=12)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
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
            rows = await probe(engine, args.report, args.apply_list,
                               args.n_quarantine, args.n_active, args.concurrency, args.seed)
            out = args.out or f"logs/known_recall-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print_summary(rows, out)
        finally:
            await engine.pg.close()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
