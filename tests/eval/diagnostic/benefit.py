"""Paired closed-book/system/gold answers, immutable raw responses and need audit.

This command intentionally calls the explicitly selected free model. It never
learns from answers, alters a memory, or calls retrieval a second time.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import shutil
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from tests.eval.diagnostic.protocol import DEFAULT, append, canonical, dump, rows, sha, verify
from tests.eval.diagnostic.service import check_block, record_block
from tests.eval.hardbench.grading import outcome
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.opencode_runner import INPUT_PROTOCOL, ask
from hooks.ce_prime_hook import format_block

SELF_REPORT = ("\nBefore the final Answer line, add two separate lines:\n"
               "Confidence: <integer 0-100, your probability the complete final answer is correct>\n"
               "Need_memory: <YES if you need more external knowledge to answer reliably, otherwise NO>\n"
               "These are your own estimates; do not use tools.")


def estimates(text: str) -> dict:
    confidence = re.findall(r"(?im)^\s*Confidence:\s*(\d{1,3})\s*%?\s*$", text)
    need = re.findall(r"(?im)^\s*Need_memory:\s*(YES|NO)\s*$", text)
    return {"confidence": min(100, int(confidence[-1])) if confidence else None,
            "self_needs_memory": need[-1].upper() == "YES" if need else None}


def job_arms(task: dict, rec: dict) -> list[str]:
    arms = ["none1"]
    if task["expected_ids"]:
        arms += ["none2", "gold"]
    if rec.get("injected_ids"):
        arms += ["system"]
    random.Random(int(sha(task["id"])[:8], 16)).shuffle(arms)
    return arms


def render_memories(ids, memories, rec):
    selected = {r["id"]: r for r in rec.get("selected", [])}
    return format_block([{"id": eid, "suggestion": memories[eid]["then"],
                          "reason": memories[eid].get("because", ""),
                          "severity": memories[eid].get("severity", "info"),
                          "confidence": memories[eid].get("confidence", .5),
                          "scope": "general" if "scope:general" in memories[eid].get("tags", []) else "specific",
                          "exploratory": selected.get(eid, {}).get("exploratory", False)} for eid in ids])


async def call(directory, key, prompt, args):
    path = directory / "api/solver" / (sha(key) + ".json")
    if path.exists():
        cached = json.loads(path.read_text())
        if cached["request_sha256"] != sha(prompt):
            raise ValueError("Cached request changed: " + key)
        record_block(directory, key, cached["response"])
        return cached["response"]
    check_block(directory)
    started = time.time()
    append(directory / "api-ledger.jsonl", {"key": key, "event": "started", "time": started,
                                            "request_sha256": sha(prompt)})
    response = await ask(prompt, args, dict(os.environ), args.opencode_bin)
    dump(path, {"key": key, "request": prompt, "request_sha256": sha(prompt), "response": response})
    append(directory / "api-ledger.jsonl", {"key": key, "event": "finished", "time": time.time(),
                                            "ok": response.get("ok"), "secs": response.get("secs")})
    record_block(directory, key, response)
    return response


async def calibrate(directory, args):
    from tests.eval.hardbench.calibrate import CASES
    controls = []
    for name, problem, reference, candidate, expected in CASES:
        prompt = JUDGE_PROMPT.format(domain="physics", problem=problem, answer=reference, pred=candidate)
        response = await call(directory, "calibration/" + name, prompt, args)
        actual = parse_verdict(response.get("text", "")) if response.get("ok") else None
        controls.append({"id": name, "expected": expected, "actual": actual, "passed": actual is expected})
    dump(directory / "grading-calibration.json", {"model": args.model, "controls": controls})
    if not all(c["passed"] for c in controls):
        raise ValueError("Grading calibration failed; inspect saved controls")


async def run(args):
    directory = args.directory.resolve()
    verify(directory / "inputs")
    model_base = args.model.split("#", 1)[0]
    if ("-free" not in args.model and not model_base.startswith("zcode/GLM-")
            and not model_base.startswith("opencode-go/glm-")):
        raise ValueError("This protocol permits an explicitly free solver or a configured GLM runner")
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    banks = json.loads((directory / "inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    recs = {r["id"]: r for r in rows(directory / "retrieval.jsonl")}
    zcode = model_base.startswith("zcode/")
    paid_glm = zcode or model_base.startswith("opencode-go/glm-")
    runner_path = Path(ask.__code__.co_filename)
    if zcode:
        from tests.eval import zcode_runner
        runner_path = Path(zcode_runner.__file__)
    manifest = {"protocol": "paired-benefit-v1", "solver": args.model,
                "input_delivery_protocol": "zcode-prompt-exact-v1" if zcode else INPUT_PROTOCOL,
                "runner_sha256": sha(runner_path.read_bytes()),
                "grader": args.model, "grader_independence": "blind separate calls, same model family; not independent human gold",
                "solver_thinking": args.thinking, "solver_max_output_tokens": args.max_output_tokens,
                "solver_timeout_s": args.timeout, "grader_max_output_tokens": 4096,
                "self_report_sha256": sha(SELF_REPORT), "judge_prompt_sha256": sha(JUDGE_PROMPT),
                "input_sha256": sha((directory / "inputs/manifest.json").read_bytes()),
                "retrieval_config_sha256": sha((directory / "retrieval-manifest.json").read_bytes()),
                "retrieval_record_identity": "each generated answer pins its complete per-question retrieval record",
                "execution_sha256": sha(Path(__file__).read_bytes()),
                "hook_formatter_sha256": sha(Path(format_block.__code__.co_filename).read_bytes()),
                "repeat_policy": "two closed-book samples on eligible gold questions; one system and one gold sample",
                "missing_memory_policy": "system with no injection reuses none1 exactly",
                "gate": "threshold chosen on calibration ids only; evaluated on disjoint holdout ids",
                "cost_requirement": ("ZCode GLM coding-plan session; cost is not exposed by the CLI"
                                     if zcode else "OpenCode Go GLM session; provider-reported cost is recorded"
                                     if paid_glm else "OpenCode -free and verified session cost=0")}
    path = directory / "benefit-manifest.json"
    if path.exists() and json.loads(path.read_text()) != manifest:
        raise ValueError("Benefit run identity changed; use another directory")
    dump(path, manifest)
    method = directory / "benefit-method"
    method.mkdir(exist_ok=True)
    for source in (Path(__file__), Path(format_block.__code__.co_filename)):
        shutil.copy2(source, method / source.name)
    baseline_manifest = directory / "baseline-manifest.json"
    if baseline_manifest.exists():
        baseline_identity = json.loads(baseline_manifest.read_text())
        for k in ("model", "thinking", "timeout", "max_output_tokens"):
            if baseline_identity[k] != getattr(args, k):
                raise ValueError("Baseline and memory solver budgets differ: " + k)
    solver = SimpleNamespace(**vars(args))
    grader = SimpleNamespace(**{**vars(args), "thinking": "low", "max_output_tokens": 4096, "timeout": 120})
    await calibrate(directory, grader)
    if args.stage == "calibrate":
        print("grading calibration passed", flush=True)
        return
    eligible = [t for t in tasks if t["solve_eligible"] and t["id"] in recs and not recs[t["id"]].get("error")]
    if args.gold_only:
        eligible = [t for t in eligible if t["expected_ids"]]
    if args.limit:
        eligible = eligible[:args.limit]
    dump(directory / f"benefit-plan-{args.arms}-{'gold' if args.gold_only else 'all'}.json",
         {"question_ids": [t["id"] for t in eligible], "arms": args.arms,
          "gold_only": args.gold_only, "code_sha256": sha(Path(__file__).read_bytes())})
    saved = {(r["id"], r["arm"]): r for r in rows(directory / "answers.jsonl")}
    semaphore = asyncio.Semaphore(args.concurrency)
    completed = 0

    async def one(task):
        nonlocal completed
        async with semaphore:
            pid, rec = task["id"], recs[task["id"]]
            for arm in job_arms(task, rec):
                if args.arms == "memory" and arm not in ("system", "gold"):
                    continue
                if (pid, arm) in saved:
                    old = saved[pid, arm]
                    if old.get("retrieval_sha256") != sha(canonical(rec)):
                        raise ValueError("Retrieval record changed for saved answer: " + pid)
                    continue
                ids = rec["injected_ids"] if arm == "system" else task["expected_ids"][:6] if arm == "gold" else []
                block = render_memories(ids, memories, rec) if ids else ""
                prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"]) + SELF_REPORT
                response = await call(directory, f"solve/{pid}/{arm}", prompt, solver)
                row = {"id": pid, "group": task["group"], "arm": arm, "injected_ids": ids,
                       "model": args.model, "response": response, "correct": None, "grade_status": "ungraded"}
                row["retrieval_sha256"] = sha(canonical(rec))
                text = response.get("text") or ""
                row.update(estimates(text))
                if not response.get("ok"):
                    if response.get("protocol_error") == "tool_attempt":
                        row.update(correct=False, judge_ok=True, grade_status="graded", graded_by="protocol")
                    elif response.get("timed_out"):
                        row.update(correct=False, judge_ok=True, grade_status="graded", graded_by="time_budget",
                                   budget_exhausted=True)
                    else:
                        row["error"] = response.get("error", "solver failure")
                else:
                    grade = grade_local(task["problem"], text)
                    need_judge = grade.pop("need_judge", False)
                    row.update(grade)
                    if need_judge:
                        # Persist the generated answer before a grader can fail.
                        append(directory / "ungraded-answers.jsonl", row)
                        gp = JUDGE_PROMPT.format(domain=task["problem"]["domain"], problem=task["problem"]["problem"],
                                                answer=task["problem"]["answer"], pred=grade["pred"])
                        gr = await call(directory, f"grade/{pid}/{arm}", gp, grader)
                        verdict = parse_verdict(gr.get("text") or "") if gr.get("ok") else None
                        row.update(correct=verdict, judge_ok=verdict is not None,
                                   grade_status="graded" if verdict is not None else "ungraded",
                                   grader_response=gr, graded_by="judge")
                    else:
                        row.update(judge_ok=True, grade_status="graded")
                append(directory / "answers.jsonl", row)
                saved[pid, arm] = row
                print(f"answer {pid} {arm} correct={row.get('correct')} error={bool(row.get('error'))}", flush=True)
            if args.arms == "all" and not rec["injected_ids"] and (pid, "system") not in saved and (pid, "none1") in saved:
                row = {**saved[pid, "none1"], "arm": "system", "reused_from": "none1"}
                append(directory / "answers.jsonl", row)
                saved[pid, "system"] = row
            completed += 1
            if completed % 10 == 0:
                print(f"benefit questions {completed}/{len(eligible)} answers={len(saved)}", flush=True)
                dump(directory / "benefit-progress.json", {"questions_this_process": completed,
                                                           "planned": len(eligible), "answers": len(saved)})
                report(directory)
    await asyncio.gather(*(one(t) for t in eligible))
    print(json.dumps(report(directory), ensure_ascii=False, indent=2), flush=True)


def classify_need(a, b, gold):
    if a is None or b is None:
        return "unknown"
    if a and b:
        return "stable_without_memory_gold_harmed" if gold is False else "stable_without_memory"
    if a != b:
        return "unstable_closed_book"
    if gold is None:
        return "unknown"
    if not a and not b:
        return "gold_rescued_both_wrong" if gold else "still_wrong_with_gold"
    raise AssertionError("Unreachable outcome combination")


def report(directory):
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    answer = {(r["id"], r["arm"]): r for r in rows(directory / "answers.jsonl")}
    result = {}
    cases = []
    for group in ("math", "physics"):
        selected = [t for t in tasks if t["group"] == group and t["solve_eligible"]]
        counts, paired = Counter(), Counter()
        for t in selected:
            pid = t["id"]
            scores = {arm: outcome(answer.get((pid, arm))) for arm in ("none1", "none2", "system", "gold")}
            if scores["none1"] is not None and scores["system"] is not None:
                a, b = scores["none1"], scores["system"]
                paired["paired"] += 1
                paired["none_correct"] += a
                paired["system_correct"] += b
                paired["system_rescued"] += not a and b
                paired["system_harmed"] += a and not b
                paired["system_injected_on_correct_base"] += a and bool((answer.get((pid, "system")) or {}).get("injected_ids"))
            label = classify_need(scores["none1"], scores["none2"], scores["gold"])
            if t["expected_ids"]:
                counts[label] += 1
            if any((pid, a) in answer for a in scores):
                cases.append({"id": pid, "group": group, "split": t["split"], "gold_ids": t["expected_ids"],
                              **scores, "need_class": label,
                              **{k: (answer.get((pid, "none1")) or {}).get(k) for k in ("confidence", "self_needs_memory")}})
        result[group] = {"eligible_questions": len(selected), "answers": sum(r["group"] == group for r in answer.values()),
                         "paired_system": dict(paired), "gold_need": dict(counts),
                         "service_errors": sum(r["group"] == group and bool(r.get("error")) for r in answer.values()),
                         "ungraded": sum(r["group"] == group and outcome(r) is None and not r.get("error") for r in answer.values())}
    # A prospective confidence gate is tuned on one split and scored on another.
    # It never reads correctness at deployment; correctness is only the metric.
    calibration = [r for r in cases if r["split"] == "calibration" and r["none1"] is not None and r["system"] is not None and r["confidence"] is not None]
    holdout = [r for r in cases if r["split"] == "holdout" and r["none1"] is not None and r["system"] is not None and r["confidence"] is not None]
    def gate_metrics(data, threshold):
        keep = lambda r: r["confidence"] < threshold
        return {"n": len(data), "retrieval_requested": sum(keep(r) for r in data),
                "correct": sum(r["system"] if keep(r) else r["none1"] for r in data),
                "none_correct": sum(r["none1"] for r in data),
                "system_correct": sum(r["system"] for r in data),
                "rescues_suppressed": sum(not keep(r) and not r["none1"] and r["system"] for r in data)}
    if calibration:
        options = [(t, gate_metrics(calibration, t)) for t in (0, 50, 60, 70, 80, 90, 95, 100, 101)]
        threshold, metrics = max(options, key=lambda item: (item[1]["correct"], -item[1]["retrieval_requested"]))
        result["confidence_gate"] = {"threshold": threshold, "calibration": metrics,
                                      "holdout": gate_metrics(holdout, threshold),
                                      "cost_caveat": "gate observes a complete no-memory draft; it does not save that draft's inference cost"}
    result["complete_recorded_cases"] = len(cases)
    dump(directory / "benefit-summary.json", result)
    (directory / "benefit-cases.jsonl").write_text("".join(canonical(r) + "\n" for r in cases))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("run", "calibrate", "report"))
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    parser.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    parser.add_argument("--opencode-bin", default="/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    parser.add_argument("--zcode-bin", default="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
    parser.add_argument("--zcode-mode", choices=("build", "plan", "guarded", "edit"), default="build")
    parser.add_argument("--thinking", default="low")
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--arms", choices=("all", "memory"), default="all")
    parser.add_argument("--gold-only", action="store_true", help="Prioritize complete bare/gold comparisons on the labelled cohort")
    args = parser.parse_args()
    if args.stage == "report":
        print(json.dumps(report(args.directory), ensure_ascii=False, indent=2))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
