"""Retry only transport/provider-failed solver rows in a saved GLM batch."""
from __future__ import annotations

import argparse
import getpass
import json
import pathlib
import shutil
import time

from tests.eval.diagnostic.benefit import SELF_REPORT, render_memories
from tests.eval.diagnostic.direct_bigmodel_pilot import MODEL, request
from tests.eval.diagnostic.protocol import dump, sha
from tests.eval.diagnostic.service import blocking_service_error
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt


def main(args):
    root = args.directory.resolve()
    answer_path = root / "answers.jsonl"
    before_path = root / "answers-before-solver-retry.jsonl"
    if not before_path.exists():
        shutil.copy2(answer_path, before_path)
    rows = [json.loads(line) for line in answer_path.read_text().splitlines() if line.strip()]
    tasks = {t["id"]: t for t in json.loads((root / "legacy/inputs/tasks.json").read_text())}
    banks = json.loads((root / "legacy/inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    recs = {}
    for variant in ("legacy", "fixed"):
        recs[variant] = {json.loads(line)["id"]: json.loads(line)
                         for line in (root / variant / "retrieval.jsonl").read_text().splitlines() if line.strip()}
    candidates = [r for r in rows if r.get("grade_status") in ("timeout", "service_error")]
    token = getpass.getpass()
    usage = {"solver_requests": 0, "solver_recovered": 0, "judge_requests": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
             "judge_prompt_tokens": 0, "judge_completion_tokens": 0, "judge_total_tokens": 0}
    stopped = False
    last_started = 0.0
    for index, row in enumerate(candidates, 1):
        task = tasks[row["id"]]
        variant = row.get("variant") or ("fixed" if row["arm"] == "fixed_system" else "legacy")
        ids = list(row.get("injected_ids") or []) if row["arm"] != "closed_book" else []
        block = render_memories(ids, memories, recs[variant][row["id"]]) if ids else ""
        prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"]) + SELF_REPORT
        delay = args.min_delay - (time.monotonic() - last_started)
        if delay > 0:
            time.sleep(delay)
        last_started = time.monotonic()
        response = request(token, prompt, max_tokens=args.max_tokens,
                           thinking=False, timeout=args.timeout)
        usage["solver_requests"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = (response.get("usage") or {}).get(key)
            if isinstance(value, (int, float)):
                usage[key] += value
        row["solver_retry_count"] = row.get("solver_retry_count", 0) + 1
        row["solver_retry_response"] = response
        if response.get("ok"):
            grade = grade_local(task["problem"], response.get("text") or "")
            if grade.pop("need_judge", False):
                judge_prompt = JUDGE_PROMPT.format(domain=task["problem"]["domain"],
                                                   problem=task["problem"]["problem"],
                                                   answer=task["problem"]["answer"], pred=grade["pred"])
                delay = args.min_delay - (time.monotonic() - last_started)
                if delay > 0:
                    time.sleep(delay)
                last_started = time.monotonic()
                judge = request(token, judge_prompt, max_tokens=args.judge_max_tokens,
                                thinking=False, timeout=args.judge_timeout)
                usage["judge_requests"] += 1
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = (judge.get("usage") or {}).get(key)
                    if isinstance(value, (int, float)):
                        usage["judge_" + key] += value
                verdict = parse_verdict(judge.get("text") or "") if judge.get("ok") else None
                grade.update(correct=verdict, judge_ok=verdict is not None,
                             grade_status="graded" if verdict is not None else "ungraded",
                             grader_response=judge, graded_by="judge")
                row["semantic_valid"] = verdict is not None
                if blocking_service_error(judge):
                    stopped = True
            else:
                grade["grade_status"] = "graded"
                row["semantic_valid"] = bool(grade.get("parsed"))
            row.update(response=response, request_sha256=sha(prompt), **grade)
            row["solver_recovered"] = True
            usage["solver_recovered"] += 1
        if blocking_service_error(response):
            stopped = True
        if index % args.progress_every == 0:
            print(f"retry {index}/{len(candidates)} recovered={usage['solver_recovered']}", flush=True)
        if stopped:
            break
    answer_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    result = {"protocol": "direct-bigmodel-solver-retry-v1", "model": MODEL,
              "candidates": len(candidates), "stopped": stopped, "usage": usage,
              "max_tokens": args.max_tokens, "judge_max_tokens": args.judge_max_tokens,
              "min_delay_s": args.min_delay}
    dump(root / "solver-retry-summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=pathlib.Path, required=True)
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--judge-max-tokens", type=int, default=1024)
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--judge-timeout", type=float, default=60)
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--progress-every", type=int, default=5)
    main(p.parse_args())
