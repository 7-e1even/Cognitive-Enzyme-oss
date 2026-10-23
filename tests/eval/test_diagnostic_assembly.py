import pytest

from tests.eval.diagnostic.assemble import self_report, valid_outcome
from tests.eval.diagnostic.benefit import classify_need
from tests.eval.diagnostic.recovery import reusable
from tests.eval.diagnostic.report import gate_audit
from tests.eval.diagnostic.service import ServiceBlocked, blocking_service_error, check_block, record_block


def test_provider_rate_limit_stops_future_calls_without_marking_model_wrong(tmp_path):
    response = {"ok": False, "error": "Rate limit exceeded. Please try again later."}
    assert blocking_service_error(response)
    with pytest.raises(ServiceBlocked):
        record_block(tmp_path, "solve/a/none1", response)
    with pytest.raises(ServiceBlocked):
        check_block(tmp_path)
    assert not blocking_service_error({"ok": False, "timed_out": True, "error": "timeout 180s"})
    assert valid_outcome({"correct": None, "grade_status": "ungraded", "response": response}) is None


def test_harness_input_fault_is_unknown_not_a_model_failure():
    assert valid_outcome({"correct": False, "grade_status": "graded",
                          "response": {"input_delivery_verified": False}}) is None


def test_two_correct_bare_answers_do_not_require_gold_to_show_observed_stability():
    assert classify_need(True, True, None) == "stable_without_memory"
    assert classify_need(True, False, None) == "unstable_closed_book"
    assert classify_need(False, False, None) == "unknown"


def test_recovery_preserves_success_and_budget_failure_but_never_reuses_service_fault():
    assert reusable({"ok": True, "input_delivery_verified": True})
    assert reusable({"ok": False, "timed_out": True})
    assert not reusable({"ok": True, "input_delivery_verified": False})
    assert not reusable({"ok": False, "error": "Rate limit exceeded"})


def test_self_report_accepts_markdown_but_rejects_invalid_probability():
    assert self_report("**Confidence:** 97%\n**Need_memory:** NO") == {
        "confidence": 97, "self_needs_memory": False}
    assert self_report("Confidence: 125\nNeed_memory: YES")["confidence"] is None


def test_gate_selection_does_not_read_holdout_correctness_or_service_unknowns():
    calibration = {"strict_input_subset": True, "split": "calibration", "none1": False,
                   "system": True, "confidence": 70}
    holdout = {**calibration, "split": "holdout", "confidence": 99}
    unknown = {**calibration, "confidence": 0, "none1": None}
    result = gate_audit([calibration, holdout, unknown])
    assert result["chosen_on_calibration"] == 80
    assert result["calibration"]["n"] == 1
    assert result["holdout"]["rescues_suppressed"] == 1
    assert result["status"] == "exploratory_only_insufficient_observed_pairs"
