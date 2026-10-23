from tests.eval.metrics import QueryOutcome, attribute_miss, evaluate


def _trace(recalled, killed=(), reranked=None, dosed=()):
    r = list(recalled)
    return {
        "recalled_ids": r,
        "condition_killed_ids": list(killed),
        "refined_ids": [i for i in r if i not in killed],
        "reranked_ids": list(reranked) if reranked is not None
                        else [i for i in r if i not in killed],
        "scored": [],
        "dosed_ids": list(dosed),
    }


def test_attribute_miss_stages():
    t = _trace(recalled=["a", "b", "c"], killed=["b"], dosed=["a"])
    assert attribute_miss("a", t, injected=["a"]) == "hit"
    assert attribute_miss("z", t, injected=["a"]) == "not_recalled"
    assert attribute_miss("b", t, injected=["a"]) == "killed_conditions"
    assert attribute_miss("c", t, injected=["a"]) == "dosage_cut"
    # 进了 dosed 但被 hook 置信度过滤
    assert attribute_miss("a", t, injected=[]) == "hook_conf_filtered"
    # 被 rerank 丢弃
    t2 = _trace(recalled=["a", "d"], reranked=["a"], dosed=["a"])
    assert attribute_miss("d", t2, injected=["a"]) == "rerank_dropped"


def test_attribute_miss_rerank_failed_overrides_downstream():
    """rerank 尝试了却失败(回退 pattern 打分)时,下游归因不可信,单列成因——
    否则这类 miss 会被误计入 dosage_cut/hook_conf_filtered。"""
    t = _trace(recalled=["a", "c"], dosed=["a"])
    t["rerank_failed"] = True
    assert attribute_miss("c", t, injected=["a"]) == "rerank_failed"
    # hit 与召回前/条件杀不受影响
    assert attribute_miss("a", t, injected=["a"]) == "hit"
    assert attribute_miss("z", t, injected=["a"]) == "not_recalled"
    # 正常跑(标记为 False/缺失)走原归因
    t["rerank_failed"] = False
    assert attribute_miss("c", t, injected=["a"]) == "dosage_cut"


def test_judge_rescue_is_not_misattributed_to_rerank():
    t = _trace(recalled=["a", "rescued"], reranked=["a"], dosed=["a"])
    t.update(jev_status="ok", jev=[("a", .9, .8), ("rescued", .2, .1)])
    assert attribute_miss("rescued", t, injected=["a"]) == "jev_cut"
    t["dosed_ids"].append("rescued")
    assert attribute_miss("rescued", t, injected=["a"]) == "hook_conf_filtered"
    assert attribute_miss("rescued", t, injected=["rescued"]) == "hit"


def test_evaluate_basic():
    outcomes = [
        QueryOutcome("q1", "positive", "math", expected=["a", "b"], acceptable=["x"],
                     injected=["a", "x", "junk"],
                     attribution={"a": "hit", "b": "not_recalled"}),
        QueryOutcome("q2", "positive", "physics", expected=["c"], acceptable=[],
                     injected=["c"], attribution={"c": "hit"}),
        QueryOutcome("q3", "silent", "", expected=[], acceptable=[],
                     injected=[], attribution={}),
        QueryOutcome("q4", "silent", "", expected=[], acceptable=[],
                     injected=["noise"], attribution={}),
        QueryOutcome("q5", "positive", "math", expected=["d"], acceptable=[],
                     injected=[], attribution={}, error="embed timeout"),
    ]
    r = evaluate(outcomes)
    # q5 有 error,不计入指标
    assert r["counts"] == {"positive": 2, "silent": 2, "error": 1}
    # micro recall = (1+1)/(2+1)
    assert abs(r["recall_at_k"] - 2 / 3) < 1e-9
    # precision = (a,x 相关 + c 相关) / (3+1 注入)
    assert abs(r["precision"] - 3 / 4) < 1e-9
    assert r["silence_rate"] == 0.5
    # MRR: q1 首个 expected 排第 1, q2 排第 1
    assert abs(r["mrr"] - 1.0) < 1e-9
    assert r["attribution_histogram"]["not_recalled"] == 1
    assert r["by_domain"]["math"]["recall_at_k"] == 0.5


def test_evaluate_empty():
    r = evaluate([])
    assert r["counts"] == {"positive": 0, "silent": 0, "error": 0}
