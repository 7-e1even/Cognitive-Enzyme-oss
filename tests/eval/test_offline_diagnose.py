"""Guard the evidence boundaries of historical math/physics diagnosis."""
from copy import deepcopy
import json
import socket
import sys

import pytest

from tests.eval.offline import diagnose as d, records


@pytest.fixture
def case():
    problem = {"id": "q", "problem": "A complete public physics question."}
    memory = {"id": "m", "then": "Use the useful identity with its conditions.",
              "version": 2, "created_at": "2026-01-01T00:00:00+00:00"}
    label = {"id": "label", "catalog": "bank", "memory_id": "m", "memory_version": 2,
             "memory_sha256": d.digest(memory["then"].encode()), "relation": "direct",
             "historical_body_requires": ["useful identity"], "rationale": "Concrete method",
             "questions": [{"id": "q", "problem_sha256": d.digest(problem["problem"].encode())}]}
    candidate = {"id": "m", "key": "a", "version": 2, "recall_rank": 3,
                 "p": .7, "q": .2, "rerank": .8}
    decision = {"id": "decision", "created_at": "2026-02-01T00:00:00+00:00",
                "context_hash": d.context_hash(problem["problem"]), "injected": [],
                "candidates": [candidate], "policy": {"jev_status": "ok"},
                "request": {"state": problem["problem"], "questions": {"a": {
                    "instructions": {"rule": {"建议": memory["then"]}}}}},
                "answers": {"best": {"probabilities": {"none": .1}}}}
    return problem, {"id": "q", "injected": []}, label, memory, decision


def witness(case, relation="direct"):
    problem, _, _, _, decision = case
    candidate = decision["candidates"][0]
    return {"question_id": problem["id"], "memory_id": candidate["id"],
            "decision_id": decision["id"], "logged_version": candidate["version"],
            "delivered_body_sha256": d.digest(d.rule_body(decision, candidate).encode()),
            "relation": relation, "rationale": "Reviewed the exact old body."}


def test_recalled_and_judged_correct_body_can_identify_selection_miss(case):
    result = d.attribute_pair(*case)
    assert result["stage"] == "selection_rejected"
    assert result["recall_rank"] == 3
    assert result["reviewed_relation"] == "direct"


def test_high_applicability_can_still_fail_the_relative_supplement_gate(case):
    decision = case[-1]
    decision["policy"]["rule"] = {"supplement_p": .85}
    candidate = decision["candidates"][0]
    candidate.update(p=.91047, q=.01671)
    decision["candidates"].append({"id": "other", "p": .92692, "q": .97736})
    facts = d.score_facts(decision, candidate)
    assert facts["passes_supplement_p"] is True
    assert facts["passes_relative_supplement_q"] is False
    assert facts["relative_supplement_q_floor"] == pytest.approx(.097736)


def test_problematic_injection_requires_agreement_and_a_reviewed_actual_body(case):
    problem, row, label, _, _ = case
    row["injected"] = ["m"]
    label["relation"] = "mixed_content"
    observation = {"id": problem["id"], "memory_id": "m", "annotation_id": "label",
                   "rationale": "Known content error", "evidence": [d.attribute_pair(*case)]}
    assert d.problematic_injections([], [observation])[0]["relations"] == ["mixed_content"]
    unknown = {**observation["evidence"][0], "reviewed_relation": None}
    observation["evidence"].append(unknown)
    assert d.problematic_injections([], [observation]) == []


def test_future_memory_and_missing_judged_candidate_are_never_recall_failures(case):
    problem, row, label, memory, decision = case
    memory["created_at"] = "2026-03-01T00:00:00+00:00"
    assert d.attribute_pair(*case)["stage"] == "not_yet_learned"
    memory["created_at"] = "2026-01-01T00:00:00+00:00"
    decision["candidates"] = []
    assert d.attribute_pair(*case)["stage"] == "upstream_unknown"
    assert d.attribute_pair(*case, changes=["2026-03-01T00:00:00+00:00"])["stage"] == "historical_version_unknown"
    # Feedback changes to updated_at alone do not imply a body edit.
    memory["updated_at"] = "2026-03-01T00:00:00+00:00"
    assert d.attribute_pair(*case)["stage"] == "upstream_unknown"


def test_truncated_memory_cannot_be_validated_by_a_keyword_in_its_prefix(case):
    problem, row, label, memory, decision = case
    decision["request"]["questions"]["a"]["instructions"]["rule"]["建议"] = "Use the useful identity"
    assert d.attribute_pair(*case)["stage"] == "historical_version_unreviewed"
    w = witness(case)
    assert d.attribute_pair(*case, witnesses=[w])["stage"] == "selection_rejected"
    w["delivered_body_sha256"] = "0" * 64
    assert d.attribute_pair(*case, witnesses=[w])["stage"] == "historical_version_unreviewed"


def test_inapplicable_old_body_cannot_inherit_a_useful_later_version(case):
    decision = case[-1]
    decision["request"]["questions"]["a"]["instructions"]["rule"]["建议"] = "An unrelated old method."
    assert d.attribute_pair(*case)["stage"] == "historical_version_unreviewed"
    assert d.attribute_pair(*case, witnesses=[witness(case, "not_applicable")])["stage"] == "historical_not_applicable"


@pytest.mark.parametrize("kind,expected", [
    ("hit", "hit"), ("post_selection", "post_selection_injection_mismatch"), ("service", "selector_service_or_trace_missing"),
    ("missing_p", "selector_service_or_trace_missing"),
    ("no_recall_rank", "judged_pool_rejected_recall_unknown"),
    ("truncated_state", "selection_input_truncated"), ("empty_state", "selection_input_unverified"),
    ("different_state", "selection_input_unverified"),
])
def test_selection_failure_is_not_confused_with_other_stages(case, kind, expected):
    _, row, _, _, decision = case
    if kind == "hit":
        row["injected"] = ["m"]
    elif kind == "post_selection":
        decision["injected"] = [{"id": "m"}]
    elif kind == "service":
        decision["policy"]["jev_status"] = "timeout"
    elif kind == "missing_p":
        decision["candidates"][0]["p"] = None
    elif kind == "no_recall_rank":
        decision["candidates"][0]["recall_rank"] = None
    else:
        decision["request"]["state"] = {"truncated_state": "A complete", "empty_state": "",
                                          "different_state": "A different question"}[kind]
    assert d.attribute_pair(*case)["stage"] == expected


def test_join_keeps_duplicate_decisions_and_never_guesses_from_partial_context(case):
    problem, row, _, _, decision = case
    duplicate = {**deepcopy(decision), "id": "second"}
    matched, mode = d.select_decisions(problem, row, [decision, duplicate])
    assert len(matched) == 2 and mode == "multiple_matching_decisions"
    decision["injected"] = [{"id": "m"}]
    assert d.select_decisions(problem, row, [decision])[1] == "unique_context_injection_difference"
    decision["context_hash"] = d.context_hash(problem["problem"][:10])
    assert d.select_decisions(problem, row, [decision]) == ([], "unresolved_decision_join")


def test_all_labels_and_historical_witnesses_are_bound_to_their_sources(case):
    problem, _, label, memory, decision = case
    spec = {"rules": [label], "historical_witnesses": [witness(case)]}
    banks, decisions = {"bank": {"m": memory}}, {"bank": {"decision": decision}}
    assert d.validate_annotations(spec, {"q": problem}, banks, decisions)["q"] == [label]
    duplicate = deepcopy(label)
    duplicate["id"] = "other"
    spec["rules"].append(duplicate)
    with pytest.raises(ValueError, match="重复题目"):
        d.validate_annotations(spec, {"q": problem}, banks, decisions)
    spec["rules"].pop()
    spec["historical_witnesses"][0]["logged_version"] = 100
    with pytest.raises(ValueError, match="历史版本证据变化"):
        d.validate_annotations(spec, {"q": problem}, banks, decisions)
    spec["historical_witnesses"] = []
    problem["problem"] += " changed"
    with pytest.raises(ValueError, match="标注题面变化"):
        d.validate_annotations(spec, {"q": problem}, banks, decisions)


def test_full_rule_witness_detects_changed_trigger_even_when_body_is_unchanged(case):
    problem, _, label, memory, decision = case
    w = witness(case)
    w["delivered_rule_sha256"] = d.rule_digest(decision, decision["candidates"][0])
    spec = {"rules": [label], "historical_witnesses": [w]}
    banks, decisions = {"bank": {"m": memory}}, {"bank": {"decision": decision}}
    d.validate_annotations(spec, {"q": problem}, banks, decisions)
    decision["request"]["questions"]["a"]["instructions"]["rule"]["触发"] = "A different precondition"
    with pytest.raises(ValueError, match="历史版本证据变化"):
        d.validate_annotations(spec, {"q": problem}, banks, decisions)


def test_current_annotation_data_does_not_turn_unknowns_or_mixed_content_into_gold():
    spec = json.loads(d.ANNOTATIONS.read_text())
    pairs = [(q["id"], r["memory_id"]) for r in spec["rules"] for q in r["questions"]]
    assert len(pairs) == len(set(pairs))
    assert "partial_positive_set" in spec["completeness"]
    assert spec["reviewer"] == "agent_content_review"
    assert all(r["relation"] in d.RELATIONS for r in spec["rules"])
    bad_aliases = {w["alias"] for w in spec["content_warnings"]}
    assert all(r["relation"] == "mixed_content" for r in spec["rules"]
               if r["id"].split("-", 1)[0] in bad_aliases)
    assert set(spec["question_issues"]["imo-bench-algebra-075"]) >= {"kind"}


def test_full_diagnosis_runs_with_network_disabled_and_verifies_exports(tmp_path, monkeypatch, case):
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("offline diagnosis opened a socket"))
    record, sources = tmp_path / "record", tmp_path / "sources"
    record.mkdir()
    sources.mkdir()
    record_manifest = {"schema": "ce-offline-records-v1", "files": {}}
    export_manifest = {"files": {}}

    def save(root, manifest, name, value, lines=False):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in value) if lines else json.dumps(value))
        manifest["files"][name] = {"sha256": d.digest(path.read_bytes())}

    problems, labels, banks = [], [], {}
    for tag, group, arm in (("math", "math", "self"), ("phys", "physics", "self_v3")):
        problem, row, label, memory, decision = deepcopy(case)
        problem["id"] = row["id"] = f"{tag}-q"
        memory["id"] = label["memory_id"] = decision["candidates"][0]["id"] = f"{tag}-m"
        label["questions"][0]["id"] = problem["id"]
        db = label["catalog"] = f"ce_rsi_{tag}_{arm}"
        label["id"] = f"{tag}-label"
        if tag == "phys":
            memory["created_at"] = "2026-03-01T00:00:00+00:00"
        save(record, record_manifest, f"hardbench/{group}/history/{tag}.meta.json", {"order": [problem["id"]]})
        save(record, record_manifest, f"hardbench/{group}/history/{tag}.{arm}.jsonl", [row], True)
        save(sources, export_manifest, f"{db}/enzymes.jsonl", [memory], True)
        save(sources, export_manifest, f"{db}/decision_log.jsonl", [decision], True)
        save(sources, export_manifest, f"{db}/evolution_log.jsonl", [], True)
        banks[db] = {"sha256": export_manifest["files"][f"{db}/enzymes.jsonl"]["sha256"]}
        problems.append(problem)
        labels.append(label)
    save(record, record_manifest, "hardbench/problems.json", problems)
    (record / "manifest.json").write_text(json.dumps(record_manifest))
    (sources / "manifest.json").write_text(json.dumps(export_manifest))
    annotations = tmp_path / "annotations.json"
    annotations.write_text(json.dumps({"rules": labels, "banks": banks, "reviewer": "test",
                                      "completeness": "partial_positive_set"}))
    summary, ledger, pairs = d.diagnose(record, sources, annotations)
    assert [p["stage"] for p in pairs] == ["selection_rejected", "not_yet_learned"]
    assert ledger[0]["expected_memory_ids"] == ["math-m"]
    assert ledger[1]["expected_memory_ids"] == []
    assert summary["api_calls"] == 0
    assert all(g["whole_dataset_recall"] is None for g in summary["groups"].values())
    assert "1 道题存在选择阶段" in d.render(summary, pairs)
    monkeypatch.setattr(sys, "argv", ["diagnose", "--record", str(record), "--output", str(tmp_path),
                                      "--annotations", str(annotations), "--sources", str(sources)])
    d.main()
    bundle = json.loads((tmp_path / "analysis_manifest.json").read_text())
    assert (tmp_path / "method/annotations.json").read_bytes() == annotations.read_bytes()
    assert all(d.digest((tmp_path / name).read_bytes()) == entry["sha256"] for name, entry in bundle["files"].items())
    (sources / "ce_rsi_math_self/decision_log.jsonl").write_text("tampered")
    with pytest.raises(ValueError, match="导出记录已变动"):
        d.diagnose(record, sources, annotations)
