from tests.eval.diagnostic.compare_fix_report import qstate


def test_service_failure_is_unknown_not_semantic_loss():
    assert qstate(None, "x") == "unmeasured"
    assert qstate({"judge_ok": False, "injected_ids": [], "p": {}}, "x") == "service_or_parse_failure"


def test_pair_state_prefers_final_injection_then_score():
    assert qstate({"judge_ok": True, "injected_ids": ["x"], "p": {"x": .2}}, "x") == "injected"
    assert qstate({"judge_ok": True, "injected_ids": [], "p": {"x": .2}}, "x") == "scored_not_selected"
    assert qstate({"judge_ok": True, "injected_ids": [], "p": {}}, "x") == "not_scored"
