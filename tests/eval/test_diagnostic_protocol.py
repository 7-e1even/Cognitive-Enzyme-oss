import json

import pytest

from tests.eval.diagnostic.protocol import exclude_sources, grams, sha, stage_for, verify
from tests.eval.diagnostic.benefit import classify_need, estimates, job_arms, render_memories
from tests.eval.opencode_runner import verify_user_input


def test_leave_question_out_excludes_updates_and_near_exemplars():
    text = "one two three four five six seven eight nine ten"
    problems = {"a": {"problem": text}, "b": {"problem": text + " eleven"}}
    history = {"a": {"lessons": [{"ids": ["updated"]}]},
               "b": {"lessons": [{"ids": ["near"]}]}}
    bank = [{"id": "exemplar"}, {"id": "other"}]
    ex = {"exemplar": [(text, grams(text))], "other": [("unrelated example", grams("unrelated example"))]}
    exclusions = exclude_sources("a", problems, history, bank,
                                 {k: grams(p["problem"]) for k, p in problems.items()}, ex)
    assert set(exclusions) == {"updated", "near", "exemplar"}


def test_embedding_rank_is_not_confused_with_ann_or_index_failure():
    rec = {"trace": {"recalled_ids": []}, "dense_searches": [
        {"indexed_ids": ["a", "b"], "hits": [["b", .9]], "exact_ranking": [["a", .95], ["b", .9]]}]}
    assert stage_for("a", rec) == "ann_miss"
    assert stage_for("missing", rec) == "index_missing"
    rec["dense_searches"][0]["exact_ranking"] = [[str(i), 1] for i in range(50)] + [["a", .1]]
    assert stage_for("a", rec) == "fused_recall_miss"


def test_judge_budget_loss_is_separate_from_score_rejection():
    rec = {"trace": {"recalled_ids": ["a"], "jev_status": "ok", "judge_pool_before_fit": ["a"], "jev_pool": []}}
    assert stage_for("a", rec) == "judge_budget_cut"
    rec["trace"]["jev_pool"] = [["a", 1]]
    assert stage_for("a", rec) == "judge_or_selection_rejected"
    rec["trace"]["dosed_ids"] = ["a"]
    assert stage_for("a", rec) == "hook_filter"


def test_model_knowing_and_gold_help_are_different_observations():
    assert classify_need(True, True, True) == "stable_without_memory"
    assert classify_need(False, False, True) == "gold_rescued_both_wrong"
    assert classify_need(False, False, False) == "still_wrong_with_gold"
    assert classify_need(True, False, True) == "unstable_closed_book"
    assert classify_need(None, False, True) == "unknown"


def test_uncertainty_is_saved_without_treating_it_as_truth():
    assert estimates("Confidence: 96\nNeed_memory: NO\nAnswer: 3") == {
        "confidence": 96, "self_needs_memory": False}
    assert estimates("Answer: 3")["confidence"] is None
    task = {"id": "q", "expected_ids": ["gold"]}
    assert set(job_arms(task, {"injected_ids": ["gold"]})) == {"none1", "none2", "system", "gold"}


def test_resume_rejects_changed_frozen_input(tmp_path):
    (tmp_path / "tasks.json").write_text("original")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {"tasks.json": sha("original")}}))
    verify(tmp_path)
    (tmp_path / "tasks.json").write_text("changed")
    with pytest.raises(ValueError, match="Frozen input changed"):
        verify(tmp_path)


def test_live_hook_marks_general_method_as_hint():
    memories = {"a": {"then": "Compare collision times", "tags": ["scope:general"], "confidence": .6}}
    block = render_memories(["a"], memories, {})
    assert "方法论提示" in block and "不是事实断言" in block


def test_durable_input_must_be_exactly_one_unquoted_prompt():
    prompt = "Solve this.\nAnswer: <number>"
    assert verify_user_input([{"type": "user", "text": prompt}], prompt)
    assert not verify_user_input([{"type": "user", "text": f'"{prompt}" "{prompt}"'}], prompt)
    assert not verify_user_input([{"type": "user", "text": prompt}, {"type": "user", "text": prompt}], prompt)
