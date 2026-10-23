"""Grade completed closed-book caches while generation continues; never resample."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from tests.eval.diagnostic.benefit import call, estimates
from tests.eval.diagnostic.protocol import DEFAULT, append, dump, rows, verify
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict


async def run(args):
    directory = args.directory.resolve()
    verify(directory / "inputs")
    tasks = {t["id"]: t for t in json.loads((directory / "inputs/tasks.json").read_text())}
    completed = {(r["id"], r["arm"]) for r in rows(directory / "baseline-evaluations.jsonl")}
    sem = asyncio.Semaphore(args.concurrency)
    async def one(raw):
        _, pid, arm = raw["key"].split("/")
        if (pid, arm) in completed:
            return
        async with sem:
            task, response = tasks[pid], raw["response"]
            row = {"id": pid, "arm": arm, "group": task["group"], "correct": None,
                   "grade_status": "ungraded", **estimates(response.get("text") or "")}
            if response.get("ok"):
                grade = grade_local(task["problem"], response.get("text") or "")
                if grade.pop("need_judge", False):
                    prompt = JUDGE_PROMPT.format(domain=task["problem"]["domain"], problem=task["problem"]["problem"],
                                                answer=task["problem"]["answer"], pred=grade["pred"])
                    result = await call(directory, f"grade/{pid}/{arm}", prompt, args)
                    correct = parse_verdict(result.get("text") or "") if result.get("ok") else None
                    grade.update(correct=correct, judge_ok=correct is not None,
                                 grade_status="graded" if correct is not None else "ungraded")
                else:
                    grade.update(judge_ok=True, grade_status="graded")
                row.update(grade)
            elif response.get("timed_out") or response.get("protocol_error") == "tool_attempt":
                row.update(correct=False, judge_ok=True, grade_status="graded",
                           budget_exhausted=bool(response.get("timed_out")),
                           protocol_error=response.get("protocol_error"))
            else:
                row["error"] = response.get("error")
            append(directory / "baseline-evaluations.jsonl", row)
            completed.add((pid, arm))
    while True:
        cached = [json.loads(p.read_text()) for p in (directory / "api/solver").glob("*.json")]
        pending = [r for r in cached if r["key"].startswith("solve/") and r["key"].endswith(("/none1", "/none2"))
                   and tuple(r["key"].split("/")[1:]) not in completed]
        await asyncio.gather(*(one(r) for r in pending))
        dump(directory / "baseline-grading-progress.json", {"evaluated": len(completed)})
        path = directory / "baseline-progress.json"
        progress = json.loads(path.read_text()) if path.exists() else {}
        if progress.get("complete") == progress.get("planned") and progress.get("planned"):
            print(f"cached baseline grading complete {len(completed)}", flush=True)
            break
        print(f"cached baseline grading {len(completed)}", flush=True)
        await asyncio.sleep(20)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=DEFAULT)
    p.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    p.add_argument("--thinking", default="low")
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--max-output-tokens", type=int, default=4096)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--opencode-bin", default="/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    asyncio.run(run(p.parse_args()))
