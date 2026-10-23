import json
import socket

from tests.eval.offline import diagnose as d, forensics as f


def test_isolated_reproductions_are_offline_and_preserve_application_files(monkeypatch):
    paths = [f.ROOT / "app/core/engine.py", f.ROOT / "app/learning/capture.py"]
    originals = [p.read_bytes() for p in paths]

    def no_socket(*a, **kw):
        raise AssertionError("offline reproduction opened a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    cache, update = f.reproduce_feedback_cache(), f.reproduce_unverified_update()
    assert cache["api_calls"] == update["api_calls"] == 0
    assert cache["expected"]["decision_id"] == "decision-A"
    assert set(cache["observed"]) == {"decision_id", "context", "applicability"}
    assert cache["source_sha256"] == d.digest(originals[0])
    assert update["source_sha256"] == d.digest(originals[1])
    assert [p.read_bytes() for p in paths] == originals


def test_raw_probability_mapping_audit_reports_corruption():
    problem = {"id": "q", "problem": "Complete public question."}
    decision = {"id": "decision", "context_hash": d.context_hash(problem["problem"]), "injected": [],
                "policy": {}, "candidates": [{"id": "m", "key": "a", "p": .7, "q": .2}],
                "request": {"state": problem["problem"], "questions": {
                    "a": {"instructions": {"rule": {"建议": "A useful method."}}}}}}
    decision["policy"]["rule"] = {"none_max": .5, "choice_min": .5, "primary_p": .3,
                                   "supplement_p": .85, "choice_override": .6, "rescue_p": .9, "rescue_q": .7}
    decision["candidates"][0]["rescued"] = False
    decision["answers"] = {"a": {"noul": .7}, "best": {"probabilities": {"a": .2, "none": .8}}}
    result = f.decision_profile([decision], {problem["id"]: problem})
    assert result["p_raw_mapping_mismatches"] == result["q_raw_mapping_mismatches"] == 0
    assert result["core_selection_exact_matches"] == 1
    decision["candidates"][0]["p"] = .8
    result = f.decision_profile([decision], {problem["id"]: problem})
    assert result["p_raw_mapping_mismatches"] == 1


def test_case_review_pins_full_rule_and_does_not_call_all_content_risks_jev_errors():
    review = json.loads(f.REVIEW.read_text())
    assert len({(r["question_id"], r["memory_id"]) for r in review["cases"]}) == len(review["cases"])
    assert all(len(r["delivered_rule_sha256"]) == 64 for r in review["cases"])
    cases = {r["question_id"]: r for r in review["cases"]}
    assert cases["physelite-9795"]["classification"] == "indirect_help_not_strong_positive"
    assert cases["physelite-1915"]["classification"] == "content_error_outside_requested_subpart"
