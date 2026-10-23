from tests.eval.diagnostic.analyze import replay_recall, score_reason, wilson


def test_recall_merge_can_drop_a_dense_hit_without_embedding_failure():
    bank = [{"id": str(i), "status": "active", "domain": "d"} for i in range(70)]
    dense = [[str(i), .9 - i*.001] for i in range(50)]
    exemplars = [{"id": str(i), "kind": "pos", "score": .99} for i in range(50, 70)]
    expected = [str(i) for i in range(50, 70)] + [str(i) for i in range(40)]
    rec = {"dense_searches": [{"hits": dense}], "exemplar_hits": exemplars,
           "trace": {"recalled_ids": expected}}
    value = replay_recall(rec, bank)
    assert value["matches"]
    assert value["full_fusion_ranking"].index("49") == 69


def test_high_absolute_probability_can_lose_to_relative_choice_gate():
    rec = {"trace": {"jev": [["a", .96, .02], ["b", .99, .97]]}}
    policy = {"selection.supplement_p": .85, "selection.primary_p": .3, "selection.none_max": .7}
    assert score_reason("a", rec, policy) == "high_p_relative_q_gate"


def test_unknown_denominator_has_no_confidence_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(10, 10)
    assert 0 < lo < hi <= 1
