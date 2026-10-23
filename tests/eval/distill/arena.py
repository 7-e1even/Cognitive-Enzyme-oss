"""老师擂台:同一批判定交给不同老师作答,按真实答案比谁更准、谁的概率更可信。

判定来源(统一成 decision 格式:state + 带类型的问题 + 真实答案):
  eval:<run>     从评测落盘还原(reports/<run>.outcomes.jsonl + 语料 + 查询),问法取 --prompt
  jsonl:<path>   decision 格式的 JSONL(app.distill.export --format arena 的输出)

老师来自 app.distill.teachers 注册表;另有伪老师 "<run>-logged":直接读评测里记下的 Jev 概率,
不重复调用。答案按 (老师, 题面哈希) 缓存到 data/cache/,中断后重跑自动续上。

指标(整体 + 分领域):
  noul AUC / Brier / ECE —— 逐条"是否有用"对真实答案的区分度与校准
  choice top1           —— 单选最高项(不含 none)是正确记忆的比例(正例查询)
  silence               —— 静默查询里单选判 none 为最高项的比例
  selection R/P/S       —— 套用线上默认选择规则后的召回 / 精度 / 静默(阈值按 Jev 标定,只作参考)

  (real_env + GLM_API_KEY) .venv-eval/bin/python -m tests.eval.distill.arena \\
      --source eval:real_jev --teachers real_jev-logged,glm-5.3 --per-domain 30 --name glm_vs_jev
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from app.distill.arena import decisions_from_jsonl, report, run_teacher, sample

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
REPORTS = HERE / "reports"
log = logging.getLogger("arena")


# ── 判定来源 ────────────────────────────────────────────────────

def decisions_from_eval(run: str, prompt: str = "v1") -> list[dict]:
    from app.typesafe_client import _PROMPTS, _rule
    from tests.eval.validate import CORPUS_DIR, QUERIES_PATH, load_corpus
    pr = _PROMPTS[prompt]
    mem = {e.id: e for e in load_corpus(CORPUS_DIR)}
    queries = {q["id"]: q for q in json.loads(Path(QUERIES_PATH).read_text(encoding="utf-8"))["queries"]}
    out = []
    for line in (HERE.parent / "reports" / f"{run}.outcomes.jsonl").read_text(encoding="utf-8").splitlines():
        o = json.loads(line)
        if not o.get("jev") or o["query_id"] not in queries:
            continue
        ids = [e for e, _, _ in o["jev"] if e in mem]
        questions = {}
        for i, eid in enumerate(ids):
            questions[f"c{i}"] = {"type": "noul",
                                  "instructions": {"rule": _rule(mem[eid], pr), "question": pr["noul"]},
                                  "criteria": {"true": pr["true"], "false": pr["false"]}}
        questions["best"] = {"type": "choice", "instructions": pr["choice"],
                             "criteria": {**{f"c{i}": _rule(mem[e], pr) for i, e in enumerate(ids)},
                                          "none": pr["none"]}}
        ok = set(o["expected"]) | set(o["acceptable"])
        logged = {f"c{i}": {"type": "noul", "noul": p} for i, (e, p, _) in enumerate(
            (e, p, q) for e, p, q in o["jev"] if e in mem)}
        qmap = {e: q for e, _, q in o["jev"]}
        logged["best"] = {"type": "choice", "probabilities": {
            **{f"c{i}": qmap[e] for i, e in enumerate(ids)}, "none": o.get("jev_none") or 0.0}}
        out.append({"id": o["query_id"], "domain": o["domain"] or "silent", "kind": o["kind"],
                    "state": queries[o["query_id"]]["text"], "questions": questions,
                    "candidates": ids, "positive": [f"c{i}" for i, e in enumerate(ids) if e in ok],
                    "expected": o["expected"], "logged": {run: logged},
                    "floor_ok": [e for e, _, _ in o.get("scored", []) if e in qmap]})
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="eval:<run> | jsonl:<path>")
    ap.add_argument("--prompt", default="v1", help="eval 来源还原题面时用的问法")
    ap.add_argument("--teachers", required=True, help="逗号分隔;<run>-logged 表示直接读评测里记下的 Jev 概率")
    ap.add_argument("--per-domain", type=int, default=0, help="每个领域抽多少次判定(0 = 全部)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(DATA / f"arena_{args.name}.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    kind, _, ref = args.source.partition(":")
    decisions = decisions_from_eval(ref, args.prompt) if kind == "eval" else decisions_from_jsonl(ref)
    decisions = sample(decisions, args.per_domain, args.seed)
    log.info("decisions: %d", len(decisions))

    from app.distill.teachers import load_teachers
    names = [t.strip() for t in args.teachers.split(",") if t.strip()]
    live = [n for n in names if not n.endswith("-logged")]
    teachers = load_teachers(live) if live else {}
    per_teacher: dict[str, dict] = {}
    for n in names:
        if n.endswith("-logged"):
            run = n[: -len("-logged")]
            per_teacher[n] = {d["id"]: d["logged"][run] for d in decisions if run in d.get("logged", {})}
        else:
            t0 = time.perf_counter()
            per_teacher[n] = await run_teacher(n, teachers[n], decisions, DATA / "cache")
            log.info("[%s] wall %.0fs", n, time.perf_counter() - t0)
    print(report(args.name, decisions, per_teacher, REPORTS))


if __name__ == "__main__":
    asyncio.run(main())
