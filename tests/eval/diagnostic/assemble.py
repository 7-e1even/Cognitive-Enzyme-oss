"""Join cached baseline grades and memory answers without making any API call."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.benefit import classify_need
from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha, verify
from tests.eval.hardbench.grading import outcome


def self_report(text):
    text = "\n".join(re.sub(r"^[\s>*#-]+", "", line).replace("**", "").replace("`", "")
                     for line in text.splitlines())
    confidence = re.findall(r"(?im)^\s*Confidence\s*:\s*(\d{1,3})\s*(?:%|/100)?\s*$", text)
    need = re.findall(r"(?im)^\s*Need_memory\s*:\s*(YES|NO)\s*$", text)
    value = int(confidence[-1]) if confidence else None
    return {"confidence": value if value is not None and 0 <= value <= 100 else None,
            "self_needs_memory": need[-1].upper() == "YES" if need else None}


def valid_outcome(row):
    if row is None:
        return None
    response = row.get("response") or {}
    if response.get("input_delivery_verified") is False or response.get("harness_error"):
        return None  # Harness faults are not model errors.
    return outcome(row)


def assemble(directory: Path):
    verify(directory / "inputs")
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    recs = {r["id"]: r for r in rows(directory / "retrieval.jsonl")}
    answers = {(r["id"], r["arm"]): r for r in rows(directory / "answers.jsonl")}
    for item in rows(directory / "baseline-evaluations.jsonl"):
        key = f"solve/{item['id']}/{item['arm']}"
        path = directory / "api/solver" / (sha(key) + ".json")
        if not path.exists():
            continue
        raw = json.loads(path.read_text())
        response = raw["response"]
        rec = recs[item["id"]]
        grade_path = directory / "api/solver" / (sha(f"grade/{item['id']}/{item['arm']}") + ".json")
        row = {**item, "response": response, "injected_ids": [],
               "retrieval_sha256": sha(canonical(rec)), "source_ledger": "baseline-evaluations.jsonl"}
        if grade_path.exists():
            row["grader_response"] = json.loads(grade_path.read_text())["response"]
        answers[item["id"], item["arm"]] = row
    quality = {r["id"]: r for r in rows(directory / "question-quality.jsonl")}
    review_path = directory / "content-review.json"
    reviews = json.loads(review_path.read_text()).get("entries", []) if review_path.exists() else []
    for review in reviews:
        task = next(t for t in tasks if t["id"] == review["id"])
        if review["question_sha256"] != sha(task["problem"]["problem"]):
            raise ValueError("Content review question changed: " + task["id"])
        if review.get("reference_sha256") and review["reference_sha256"] != sha(task["problem"]["answer"]):
            raise ValueError("Content review reference changed: " + task["id"])
    manually_bad = {r["id"] for r in reviews if r.get("exclude_from_need") or r["status"].startswith(("incomplete", "reference_disputed"))}
    cases = []
    totals = {}
    for task in tasks:
        if not task["solve_eligible"]:
            continue
        pid, rec = task["id"], recs[task["id"]]
        if not rec["injected_ids"] and (pid, "none1") in answers:
            answers[pid, "system"] = {**answers[pid, "none1"], "arm": "system", "reused_from": "none1"}
        values = {a: valid_outcome(answers.get((pid, a))) for a in ("none1", "none2", "system", "gold")}
        base = answers.get((pid, "none1")) or {}
        q = quality.get(pid)
        assessable = bool(q and q["completeness"] == "usable" and not q["solution_in_question"] and pid not in manually_bad)
        if values["none1"] is not None and values["none2"] is not None:
            bare = "both_correct" if values["none1"] and values["none2"] else "both_failed" if not values["none1"] and not values["none2"] else "unstable"
        else:
            bare = "not_two_valid_samples"
        fields = self_report((base.get("response") or {}).get("text") or "")
        case = {"id": pid, "group": task["group"], "split": task["split"],
                "gold_ids": task["expected_ids"], "system_ids": rec["injected_ids"],
                **values, **fields, "bare_status": bare,
                "need_class": classify_need(values["none1"], values["none2"], values["gold"]),
                "question_audit": q["completeness"] if q else "not_reviewed",
                "solution_in_question": q.get("solution_in_question") if q else None,
                "manually_confirmed_input_defect": pid in manually_bad,
                "strict_input_subset": assessable}
        for arm in values:
            row = answers.get((pid, arm)) or {}
            r = row.get("response") or {}
            case[arm + "_budget_exhausted"] = bool(r.get("timed_out") or r.get("stop") == "length")
            case[arm + "_secs"] = r.get("secs")
            case[arm + "_tool_attempt"] = r.get("protocol_error") == "tool_attempt"
        if any((pid, arm) in answers for arm in values):
            cases.append(case)
    for group in ("math", "physics"):
        by_group = [r for r in cases if r["group"] == group]
        cohorts = {}
        for name, cohort in (("all_recorded", by_group), ("strict_input_subset", [r for r in by_group if r["strict_input_subset"]])):
            paired = [r for r in cohort if r["none1"] is not None and r["system"] is not None]
            gold = [r for r in cohort if r["gold_ids"]]
            observed = Counter(r["need_class"] for r in gold)
            cohorts[name] = {"recorded_questions": len(cohort), "paired_system": len(paired),
                             "none_correct": sum(r["none1"] for r in paired),
                             "system_correct": sum(r["system"] for r in paired),
                             "observed_system_rescues": sum(not r["none1"] and r["system"] for r in paired),
                             "observed_system_harms": sum(r["none1"] and not r["system"] for r in paired),
                             "gold_need": dict(observed),
                             "bare_repeats": dict(Counter(r["bare_status"] for r in gold)),
                             "complete_gold_triples": sum(all(r[a] is not None for a in ("none1", "none2", "gold")) for r in gold),
                             "gold_rescues_with_baseline_budget_exhaustion": sum(r["need_class"] == "gold_rescued_both_wrong" and (r["none1_budget_exhausted"] or r["none2_budget_exhausted"]) for r in gold),
                             "baseline_budget_failures": sum(r["none1"] is False and r["none1_budget_exhausted"] for r in cohort),
                             "unknown_model_or_grade": sum(r["none1"] is None for r in cohort)}
        totals[group] = cohorts
    dump(directory / "assembled-summary.json", totals)
    (directory / "assembled-cases.jsonl").write_text("".join(canonical(r) + "\n" for r in cases))
    (directory / "assembled-answers.jsonl").write_text("".join(canonical(r) + "\n" for r in answers.values()))
    dump(directory / "assembly-method.json", {"code_sha256": sha(Path(__file__).read_bytes()),
                                               "model_rows_source": "answers.jsonl", "baseline_grades_source": "baseline-evaluations.jsonl",
                                               "source_files_sha256": {n: sha((directory/n).read_bytes()) for n in ("answers.jsonl", "baseline-evaluations.jsonl") if (directory/n).exists()}})
    return totals


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=DEFAULT)
    print(json.dumps(assemble(p.parse_args().directory), ensure_ascii=False, indent=2))
