"""Small paired GLM answer pilot for one frozen legacy/fixed question."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from tests.eval.diagnostic.benefit import SELF_REPORT, estimates, render_memories
from tests.eval.diagnostic.protocol import append, canonical, dump, rows, sha
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.opencode_runner import ask


async def run(args):
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    variants = {v: root / v for v in ("legacy", "fixed")}
    tasks = json.loads((variants["legacy"] / "inputs/tasks.json").read_text())
    task = next(t for t in tasks if t["id"] == args.question_id)
    banks = json.loads((variants["legacy"] / "inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    recs = {v: {r["id"]: r for r in rows(variants[v] / "retrieval.jsonl")} for v in variants}
    out = root / "responses.jsonl"
    done = {(r["variant"], r["arm"]) for r in rows(out)}
    call_args = type("Args", (), {"model": args.model, "thinking": "low",
                                   "max_output_tokens": args.max_output_tokens,
                                   "timeout": args.timeout})()
    answers = {}

    async def one(variant, arm):
        key = (variant, arm)
        if key in done:
            return
        rec = recs[variant][args.question_id]
        ids = list(rec.get("injected_ids") or []) if arm == "system" else []
        block = render_memories(ids, memories, rec) if ids else ""
        prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"]) + SELF_REPORT
        response = await ask(prompt, call_args, dict(os.environ), args.opencode_bin)
        row = {"question_id": args.question_id, "variant": variant, "arm": arm,
               "model": args.model, "injected_ids": ids, "request_sha256": sha(prompt),
               "response": response, **estimates(response.get("text") or "")}
        if response.get("ok"):
            grade = grade_local(task["problem"], response.get("text") or "")
            if grade.pop("need_judge", False):
                judge_args = type("JudgeArgs", (), {"model": args.model, "thinking": "low",
                                                     "max_output_tokens": 4096,
                                                     "timeout": args.judge_timeout})()
                judge_prompt = JUDGE_PROMPT.format(domain=task["problem"]["domain"],
                                                   problem=task["problem"]["problem"],
                                                   answer=task["problem"]["answer"],
                                                   pred=grade["pred"])
                judge = await ask(judge_prompt, judge_args, dict(os.environ), args.opencode_bin)
                grade.update(correct=parse_verdict(judge.get("text") or "") if judge.get("ok") else None,
                             judge_ok=bool(judge.get("ok")), grade_status="graded" if judge.get("ok") else "ungraded",
                             grader_response=judge, graded_by="judge")
            else:
                grade["grade_status"] = "graded"
            row.update(grade)
        elif response.get("timed_out"):
            row.update(correct=False, judge_ok=True, grade_status="graded", graded_by="time_budget")
        else:
            row["error"] = response.get("error")
        append(out, row)

    # One shared closed-book sample, then both selector variants.
    await one("legacy", "none1")
    legacy_none = next((r for r in rows(out) if r["variant"] == "legacy" and r["arm"] == "none1"), None)
    if legacy_none and ("fixed", "none1") not in done:
        append(out, {**legacy_none, "variant": "fixed", "reused_from": "legacy/none1"})
    await one("legacy", "system")
    await one("fixed", "system")
    rows_now = rows(out)
    by = {(r["variant"], r["arm"]): r for r in rows_now}
    status = {}
    for variant in variants:
        for arm in ("none1", "system"):
            r = by.get((variant, arm))
            status[f"{variant}_{arm}"] = {
                "correct": r.get("correct") if r else None,
                "grade_status": r.get("grade_status") if r else "missing",
                "error": r.get("error") if r else None,
                "secs": (r.get("response") or {}).get("secs") if r else None,
                "cost": (r.get("response") or {}).get("cost") if r else None,
            }
    valid = all(status[k]["correct"] in (True, False) for k in ("legacy_none1", "legacy_system", "fixed_system"))
    summary = {"protocol": "glm-pilot-v1", "question_id": args.question_id,
               "model": args.model, "status": status, "paired_valid": valid,
               "legacy_rescue": status["legacy_none1"]["correct"] is False and status["legacy_system"]["correct"] is True if valid else None,
               "fixed_rescue": status["legacy_none1"]["correct"] is False and status["fixed_system"]["correct"] is True if valid else None,
               "fixed_harm": status["legacy_none1"]["correct"] is True and status["fixed_system"]["correct"] is False if valid else None,
               "api_calls_recorded": sum(1 for _ in rows(out))}
    dump(root / "summary.json", summary)
    (root / "report.md").write_text("# GLM 小范围 system/closed-book 对照\n\n" +
        f"题目：`{args.question_id}`；模型：`{args.model}`。\n\n" +
        "| 臂 | correct | 状态 | 秒数 | cost |\n|---|---|---|---:|---:|\n" +
        "\n".join(f"| {k} | {v['correct']} | {v['grade_status']} | {v['secs']} | {v['cost']} |" for k,v in status.items()) +
        "\n\n只有三臂都有有效判分时，rescue/harm 才有意义。该 pilot 只用于验证 GLM 入口和答案层对照，不外推 92 题。\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--question-id", default="physelite-11041")
    p.add_argument("--model", default="opencode-go/glm-5.3-flash")
    p.add_argument("--opencode-bin", default="/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--judge-timeout", type=float, default=60)
    p.add_argument("--max-output-tokens", type=int, default=4096)
    asyncio.run(run(p.parse_args()))
