"""Does the injected memory make the model answer correctly? Paired closed-book / system / gold answers.

Question set (fixed before any new answer, from historical closed-book runs only):
  * physics questions GLM-5.3 got wrong in both historical closed-book runs (base, base2);
  * math questions it got wrong in the historical closed-book run;
  * a hash-sampled control set of physics questions it got right in both runs (measures harm).
Arms: ``none`` (fresh closed-book), one per retrieval arm (injected ids from that arm's record),
and ``gold`` (annotated memories). Identical prompts share one answer, so an arm that injects
nothing reuses ``none``. Solver and grader: pi + GLM-5.3 (``--model``), no tools, empty cwd.

    .venv-eval/bin/python -m tests.eval.diagnostic.uplift run --arms none,gold,collected
    .venv-eval/bin/python -m tests.eval.diagnostic.uplift report
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from tests.eval.diagnostic.benefit import render_memories
from tests.eval.diagnostic.protocol import EVAL, append, rows, sha
from tests.eval.hardbench.calibrate import CASES
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.realbench.uplift_eval import RATE_LIMIT, ask_pi, mcnemar_p

SOURCE = EVAL / "offline/data/collection-2026-10-16/formal"
OUT = EVAL / "offline/data/collection-2026-10-23"
RSI = EVAL / "hardbench/data/rsi"
ANSWERS = OUT / "uplift/answers.jsonl"   # replaced per solver model in main()


def model_dir(model: str) -> Path:
    return OUT / "uplift" / model.replace("/", "_")
CONTROL_N = 40


COHORTS: set[str] | None = None   # set in main(); None = all cohorts


def question_set() -> dict[str, str]:
    """id → cohort, chosen only from historical closed-book outcomes (never from new answers)."""
    tasks = {t["id"]: t for t in json.loads((SOURCE / "inputs/tasks.json").read_text())}

    def load(name):
        return {r["id"]: r.get("correct") for r in rows(RSI / name)}
    b1, b2, m = load("phys.base.jsonl"), load("phys.base2.jsonl"), load("math.base.jsonl")
    cohort = {}
    right = []
    for pid, t in tasks.items():
        if not t["solve_eligible"]:
            continue
        if t["group"] == "physics" and b1.get(pid) is False and b2.get(pid) is False:
            cohort[pid] = "phys_wrong"
        elif t["group"] == "math" and m.get(pid) is False:
            cohort[pid] = "math_wrong"
        elif t["group"] == "physics" and b1.get(pid) is True and b2.get(pid) is True:
            right.append(pid)
    right.sort(key=lambda pid: sha("control|" + pid))
    cohort.update({pid: "phys_right" for pid in right[:CONTROL_N]})
    if COHORTS is not None:
        cohort = {pid: c for pid, c in cohort.items() if c in COHORTS}
    return cohort


def injections(arm: str) -> dict[str, list[str]]:
    if arm == "collected":
        return {r["id"]: r["injected_ids"] for r in rows(SOURCE / "retrieval.jsonl") if not r.get("error")}
    from tests.eval.diagnostic.arms import degraded
    out: dict[str, list[str]] = {}
    for r in rows(OUT / "arms" / f"{arm}.jsonl"):   # a degraded row (rerank/Jev failure) is never a result
        if not degraded(r):
            out[r["id"]] = r["injected_ids"]
    return out


def ids_for(arm: str, task: dict, inj: dict) -> list[str] | None:
    if arm == "none":
        return []
    if arm == "gold":
        return task["expected_ids"][:6] if task["expected_ids"] else None
    return inj[arm].get(task["id"])


def answer_key(pid: str, ids: list[str]) -> str:
    return pid + "|" + ",".join(ids)


_TOOL_TEXT = ("<tool_call>", "invoke name=", "]<]minimax[>[")


async def solve(prompt: str, args, cwd: Path) -> dict:
    """One answer; rate limits back off, a time-budget overrun is a failed answer (not retried).

    MiniMax sometimes emits a pseudo tool call as text although no tools exist; that is a
    protocol failure of the sampling, not an answer, so it is resampled (counted in res["tool_text_retries"])."""
    waited, tries, tool_retries = 0.0, 0, 0
    while True:
        res = await ask_pi(prompt, args, dict(__import__("os").environ), cwd)
        if res.get("ok") and any(m in (res.get("text") or "") for m in _TOOL_TEXT) and tool_retries < 3:
            tool_retries += 1
            continue
        if res.get("ok"):
            res["tool_text_retries"] = tool_retries
            return res
        err = res.get("error", "")
        if err.startswith("timeout"):
            return {**res, "ok": True, "text": "", "stop": "time_budget", "budget_exhausted": True}
        if RATE_LIMIT.search(err) and waited < args.max_wait:
            await asyncio.sleep(60)
            waited += 60
            continue
        tries += 1
        if tries > 2:
            return res
        await asyncio.sleep(10 * tries)


async def grade(task: dict, text: str, judge, cwd: Path) -> dict:
    p = task["problem"]
    g = grade_local(p, text)
    if not g.pop("need_judge", False):
        return g
    prompt = JUDGE_PROMPT.format(domain=p["domain"], problem=p["problem"], answer=p["answer"], pred=g["pred"])
    for _ in range(3):
        jr = await solve(prompt, judge, cwd)
        verdict = parse_verdict(jr.get("text") or "") if jr.get("ok") else None
        if verdict is not None:
            return {**g, "correct": verdict, "graded_by": "judge", "judge_text": (jr.get("text") or "")[-300:]}
    return {**g, "correct": None, "graded_by": "judge_failed"}


async def calibrate(judge, cwd: Path) -> None:
    path = model_dir(judge.model) / "judge-calibration.json"
    if path.exists() and json.loads(path.read_text()).get("model") == judge.model:
        return
    results = []
    for name, problem, reference, candidate, expected in CASES:
        res = await solve(JUDGE_PROMPT.format(domain="physics", problem=problem, answer=reference, pred=candidate),
                          judge, cwd)
        got = parse_verdict(res.get("text") or "")
        results.append({"case": name, "expected": expected, "got": got})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": judge.model, "results": results}, indent=2))
    bad = [r for r in results if r["got"] is not r["expected"]]
    if bad:
        raise SystemExit(f"judge calibration failed: {bad}")


async def run(args) -> None:
    tasks = {t["id"]: t for t in json.loads((SOURCE / "inputs/tasks.json").read_text())}
    banks = json.loads((SOURCE / "inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    cohort = question_set()
    arms = args.arms.split(",")
    inj = {a: injections(a) for a in arms if a not in ("none", "gold")}
    cwd = OUT / "uplift/pi_cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    solver = SimpleNamespace(model=args.model, thinking="high", timeout=args.timeout, max_wait=args.max_wait)
    judge = SimpleNamespace(model=args.judge_model or args.model, thinking="low", timeout=300,
                            max_wait=args.max_wait)
    await calibrate(judge, cwd)
    done = {r["key"]: r for r in rows(ANSWERS)
            if (r.get("correct") is not None or r.get("budget_exhausted"))
            and not any(m in (r.get("text") or "") for m in _TOOL_TEXT)}
    order = sorted(cohort, key=lambda pid: sha("order|" + pid))
    jobs = []
    for pid in order:
        for arm in arms:
            ids = ids_for(arm, tasks[pid], inj)
            if ids is None:
                continue
            key = answer_key(pid, ids)
            if key not in done and key not in {k for k, *_ in jobs}:
                jobs.append((key, pid, ids))
    if args.limit:
        jobs = jobs[:args.limit]
    print(f"questions={len(cohort)} {dict(Counter(cohort.values()))} arms={arms} new answers={len(jobs)}", flush=True)
    sem = asyncio.Semaphore(args.concurrency)
    n = 0

    async def one(key, pid, ids):
        nonlocal n
        async with sem:
            task = tasks[pid]
            block = render_memories(ids, memories, {"selected": []}) if ids else ""
            prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"])
            res = await solve(prompt, solver, cwd)
            row = {"key": key, "id": pid, "cohort": cohort[pid], "ids": ids, "prompt_sha256": sha(prompt),
                   "model": args.model, "judge_model": judge.model, "secs": res.get("secs"), "usage": res.get("usage"),
                   "stop": res.get("stop"), "text": (res.get("text") or "")[-4000:], "time": time.time(),
                   "tool_text_retries": res.get("tool_text_retries", 0)}
            if not res.get("ok"):
                row.update(correct=None, error=res.get("error", "")[:300])
            elif res.get("budget_exhausted"):
                row.update(correct=False, graded_by="time_budget", budget_exhausted=True)
            else:
                row.update(await grade(task, res.get("text") or "", judge, cwd))
            append(ANSWERS, row)
            n += 1
            print(f"[{n}/{len(jobs)}] {pid} inj={len(ids)} correct={row.get('correct')} {row.get('graded_by', '')} "
                  f"{row.get('secs')}s {row.get('error', '')[:80]}", flush=True)
    await asyncio.gather(*(one(*j) for j in jobs))


def report(arms: list[str]) -> str:
    tasks = {t["id"]: t for t in json.loads((SOURCE / "inputs/tasks.json").read_text())}
    cohort = question_set()
    ans = {}
    for r in rows(ANSWERS):   # last valid row per key wins; pseudo tool-call text is never an answer
        if r.get("correct") is not None and not any(m in (r.get("text") or "") for m in _TOOL_TEXT):
            ans[r["key"]] = r
    inj = {a: injections(a) for a in arms if a not in ("none", "gold")}
    out = [f"# 注入收益:同一题闭卷 vs 注入({ANSWERS.parent.name})", "",
           "只统计闭卷与该组都有有效判分的题;注入为空的题该组答案即闭卷答案。", ""]
    for name in ("phys_wrong", "math_wrong", "phys_right", "all"):
        ids = [pid for pid, c in cohort.items() if name == "all" or c == name]
        out += [f"## {name}({len(ids)} 题)", "",
                "| 组 | 配对题 | 闭卷对 | 本组对 | 救回 | 带错 | McNemar p | 有注入的题 | 注入含正例 |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for arm in arms:
            if arm == "none":
                continue
            c = Counter()
            for pid in ids:
                base = ans.get(answer_key(pid, []))
                got = ids_for(arm, tasks[pid], inj)
                if base is None or got is None:
                    continue
                r = ans.get(answer_key(pid, got))
                if r is None:
                    continue
                a, b = base["correct"], r["correct"]
                c["n"] += 1
                c["a"] += a
                c["b"] += b
                c["win"] += (not a) and b
                c["loss"] += a and not b
                c["inj"] += bool(got)
                c["gold"] += bool(set(got) & set(tasks[pid]["expected_ids"]))
            if c["n"]:
                p = mcnemar_p(c["win"], c["loss"])
                out.append(f"| {arm} | {c['n']} | {c['a']} | {c['b']} | {c['win']} | {c['loss']} | {p:.3f} | "
                           f"{c['inj']} | {c['gold']} |")
        out.append("")
    budget = sum(1 for r in ans.values() if r.get("budget_exhausted"))
    out.append(f"有效答案 {len(ans)} 条,其中超时计错 {budget} 条。")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stage", choices=("run", "report", "plan"))
    ap.add_argument("--arms", default="none,gold,collected")
    ap.add_argument("--model", default="magpie/minimax-cn/MiniMax-M3.1-Flash")
    ap.add_argument("--judge-model", default="", help="grader; empty = same as --model")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--max-wait", type=float, default=3 * 3600)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cohorts", default="phys_wrong,math_wrong",
                    help="comma list of phys_wrong,math_wrong,phys_right (control); default skips the control")
    args = ap.parse_args()
    global ANSWERS, COHORTS
    COHORTS = set(args.cohorts.split(","))
    ANSWERS = model_dir(args.model) / "answers.jsonl"
    if args.stage == "plan":
        c = question_set()
        print(len(c), Counter(c.values()))
    elif args.stage == "report":
        print(report(args.arms.split(",")))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
