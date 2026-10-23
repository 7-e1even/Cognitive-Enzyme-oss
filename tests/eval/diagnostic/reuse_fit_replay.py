"""Replay the small scan12 cohort with saved p/q answers and no network calls."""
from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import canonical, dump, rows
from tests.eval.diagnostic import two_stage as ts


def answer_for_key(key: str, value: float, *, kind: str = "noul"):
    return {"type": kind, "noul": value} if kind == "noul" else value


async def replay(directory: Path):
    from app import settings
    import app.typesafe_client as tc
    from app.config import TypeSafeConfig
    from app.matching import pipeline as pl

    directory = directory.resolve()
    plan, tasks, recs, banks, metadata = ts.load(directory)
    baseline = {(r["id"], r["arm"]): r for r in rows(Path(plan["baseline_results"]))}
    settings._values.clear()
    settings._values.update(metadata["policy"])
    settings._values["judge.two_stage"] = True
    settings._values["judge.scan_reuse_fit"] = True
    settings._values["judge.scan_pool"] = 12
    output = directory / "replay-results.jsonl"
    output.write_text("")
    records = []
    original_system_one = tc.system_one
    try:
        for pid in plan["question_ids"]:
            task, rec = tasks[pid], recs[pid]
            restored = ts.restore(task, rec, banks)
            single = baseline[(pid, "single")]
            scan12 = baseline[(pid, "scan12")]
            memories = restored["memories"]
            # Current prompt builders are used to map rule objects back to ids;
            # no saved request is treated as an instruction.
            rule_to_id = {}
            for enzyme in memories.values():
                for prompt in (tc._prompt_pair()[0], tc._prompt_pair()[1]):
                    rule_to_id[canonical(tc._rule(enzyme, prompt))] = enzyme.id
            async def fake_system_one(state, questions, config, timeout=0):
                answers = {}
                for key, question in questions.items():
                    if question.get("type") == "noul":
                        rule = question.get("instructions", {}).get("rule")
                        eid = rule_to_id.get(canonical(rule))
                        source = single if eid in single["p"] else scan12
                        answers[key] = {"type": "noul", "noul": float(source["p"].get(eid, .0))}
                    elif question.get("type") == "choice":
                        criteria = question.get("criteria") or {}
                        probs = {}
                        for option, rule in criteria.items():
                            if option == "none":
                                continue
                            eid = rule_to_id.get(canonical(rule))
                            source = single if eid in single["q"] else scan12
                            probs[option] = float(source["q"].get(eid, .0))
                        candidate_ids = {rule_to_id.get(canonical(rule)) for option, rule in criteria.items()
                                         if option != "none"}
                        source = single if candidate_ids <= set(single["q"]) else scan12
                        probs["none"] = float(source.get("none") or 0.0)
                        answers[key] = {"type": "choice", "probabilities": probs}
                return {"answers": answers, "model": "saved-replay"}

            tc.system_one = fake_system_one
            app = await pl.judge_two_stage(task["problem"]["problem"], restored["pool"],
                                           TypeSafeConfig(api_key="offline"),
                                           analogies=restored["analogies"])
            selected, injected = ts.select(app, restored, metadata["max_results"])
            row = {"id": pid, "arm": "reuse12_replay", "judge_ok": app is not None,
                   "logical_calls": app.calls if app else None, "injected_ids": injected,
                   "selected_ids": [r.enzyme.id for r in selected], "p": app.p if app else {},
                   "q": app.q if app else {}, "none": app.none if app else None}
            records.append(row)
            with output.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    finally:
        tc.system_one = original_system_one

    by = {r["id"]: r for r in records}
    target = plan["target_pairs"]
    transitions = Counter()
    for pair in target:
        old = pair["memory_id"] in baseline[(pair["id"], "scan12")]["injected_ids"]
        new = pair["memory_id"] in by[pair["id"]]["injected_ids"]
        transitions[f"{old}->{new}"] += 1
    controls = []
    for pid in plan["control_question_ids"]:
        old = set(baseline[(pid, "scan12")]["injected_ids"])
        new = set(by[pid]["injected_ids"])
        controls.append({"id": pid, "old": sorted(old), "new": sorted(new),
                         "lost": sorted(old - new), "gained": sorted(new - old)})
    result = {"protocol": "frozen-candidate-reuse-fit-replay-v1", "questions": len(records),
              "network_calls": 0, "embedding_calls": 0, "rerank_calls": 0,
              "judge_successes": sum(r["judge_ok"] for r in records),
              "logical_calls": dict(Counter(r["logical_calls"] for r in records)),
              "target_injection_transitions": dict(transitions), "controls": controls,
              "note": "Saved scan12 p/q responses replayed to validate selection mechanics; this is not a fresh provider latency trial."}
    dump(directory / "replay-summary.json", result)
    (directory / "replay-report.md").write_text(
        "# 复用原判定池的离线回放\n\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(replay(args.directory)), ensure_ascii=False, indent=2))
