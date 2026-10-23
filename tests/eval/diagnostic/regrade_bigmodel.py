"""Re-grade existing BigModel answers without re-running the solver."""
from __future__ import annotations

import argparse
import getpass
import json
import pathlib
import shutil
import time

from tests.eval.diagnostic.direct_bigmodel_pilot import MODEL, request
from tests.eval.diagnostic.protocol import dump
from tests.eval.diagnostic.service import blocking_service_error
from tests.eval.hardbench.run import JUDGE_PROMPT, parse_verdict


def main(args):
    root = args.directory.resolve()
    answer_path = root / "answers.jsonl"
    before_path = root / "answers-before-regrade.jsonl"
    if not before_path.exists():
        shutil.copy2(answer_path, before_path)
    rows = [json.loads(line) for line in answer_path.read_text().splitlines() if line.strip()]
    tasks = {t["id"]: t for t in json.loads((root / "legacy/inputs/tasks.json").read_text())}
    candidates = [r for r in rows if r.get("grade_status") == "ungraded"
                  and (r.get("response") or {}).get("ok") and r.get("pred")]
    token = getpass.getpass()
    usage = {"requests": 0, "recovered": 0, "failed": 0,
             "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    stopped = False
    last_started = 0.0
    for index, row in enumerate(candidates, 1):
        task = tasks[row["id"]]
        prompt = JUDGE_PROMPT.format(domain=task["problem"]["domain"],
                                     problem=task["problem"]["problem"],
                                     answer=task["problem"]["answer"], pred=row["pred"])
        delay = args.min_delay - (time.monotonic() - last_started)
        if delay > 0:
            time.sleep(delay)
        last_started = time.monotonic()
        judge = request(token, prompt, max_tokens=args.max_tokens,
                        thinking=False, timeout=args.timeout)
        usage["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = (judge.get("usage") or {}).get(key)
            if isinstance(value, (int, float)):
                usage[key] += value
        verdict = parse_verdict(judge.get("text") or "") if judge.get("ok") else None
        row["grader_retry_count"] = row.get("grader_retry_count", 0) + 1
        row["grader_retry_response"] = judge
        if verdict is not None:
            row.update(correct=verdict, semantic_valid=True,
                       grade_status="graded", graded_by="judge",
                       grader_response=judge, grader_recovered=True)
            usage["recovered"] += 1
        else:
            usage["failed"] += 1
        if blocking_service_error(judge):
            stopped = True
            break
        if index % args.progress_every == 0:
            print(f"regrade {index}/{len(candidates)} recovered={usage['recovered']}", flush=True)
    answer_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    result = {"protocol": "direct-bigmodel-regrade-v1", "model": MODEL,
              "candidates": len(candidates), "stopped": stopped, "usage": usage,
              "max_tokens": args.max_tokens, "min_delay_s": args.min_delay}
    dump(root / "regrade-summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=pathlib.Path, required=True)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--min-delay", type=float, default=1.0)
    p.add_argument("--progress-every", type=int, default=10)
    main(p.parse_args())
