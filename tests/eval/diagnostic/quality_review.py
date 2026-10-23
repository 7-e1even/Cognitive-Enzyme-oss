"""Recover typed audit objects from saved output; no model or network access."""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha


def typed(value, expected):
    return (isinstance(value, dict) and value.get("id") in expected
            and value.get("completeness") in ("usable", "incomplete", "uncertain")
            and isinstance(value.get("solution_in_question"), bool))


def recover(text, expected):
    decoder = json.JSONDecoder()
    candidates = defaultdict(list)
    # Every recovered object must be complete JSON with a known id and typed
    # fields. Incomplete trailing objects and prose are never filled in.
    for match in re.finditer(r'\{\s*"id"\s*:', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if typed(value, expected):
            candidates[value["id"]].append(value)
    result = {}
    for pid, values in candidates.items():
        signatures = {(v["completeness"], v["solution_in_question"]) for v in values}
        if len(signatures) == 1:
            result[pid] = {**values[0], "duplicate_objects": len(values)}
        else:
            result[pid] = {"id": pid, "completeness": "uncertain", "solution_in_question": False,
                           "missing": "", "solution_evidence": "", "reason": "conflicting complete audit objects",
                           "audit_conflict": True}
    return result


def prepare(directory):
    # The campaign parent contains the archived argv pilot. Only its question
    # audit is reused; model performance answers are never imported.
    pilot = directory.parent
    tasks = {t["id"]: t for t in json.loads((directory / "inputs/tasks.json").read_text())}
    old = {r["id"]: r for r in rows(pilot / "question-quality.jsonl")}
    recovered = {}
    for p in (pilot / "api/solver").glob("*.json"):
        raw = json.loads(p.read_text())
        if not raw["key"].startswith("quality/"):
            continue
        try:
            items = json.loads(raw["request"].split("Items:\n", 1)[1])
        except (ValueError, IndexError):
            continue
        expected = {t["id"] for t in items}
        values = recover(raw["response"].get("text") or "", expected)
        for pid, judgment in values.items():
            recovered[pid] = {**judgment, "group": tasks[pid]["group"],
                              "question_sha256": sha(tasks[pid]["problem"]["problem"]),
                              "reference_sha256": sha(tasks[pid]["problem"]["answer"]),
                              "review_status": "agent_followup_required", "source_protocol": "pilot argv repeated input; blind input audit only",
                              "source_request": raw["key"]}
    ready = {}
    for pid in tasks:
        value = recovered.get(pid) or old.get(pid)
        if value and value.get("completeness") != "uncertain" and not value.get("audit_error"):
            ready[pid] = {**value, "source_protocol": "pilot argv repeated input; blind input audit only"}
    target = directory / "question-quality.jsonl"
    if target.exists():
        existing = {r["id"]: r for r in rows(target)}
        ready.update(existing)
    target.write_text("".join(canonical(r) + "\n" for r in ready.values()))
    dump(directory / "quality-recovery.json", {"recovered_objects": len(recovered), "reused_nonuncertain": len(ready),
                                                "remaining_for_exact_stdin_audit": len(tasks)-len(ready),
                                                "code_sha256": sha(Path(__file__).read_bytes())})
    return len(ready), len(tasks)-len(ready)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, default=DEFAULT)
    print(prepare(p.parse_args().directory))
