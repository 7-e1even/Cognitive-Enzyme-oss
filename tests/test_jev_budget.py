"""判官请求体积控制:字段截断(then 400 / because 150 / state 1500)与 token 预算
(judge.token_budget — 按精排序留最长候选前缀,类比题先丢)。

bocha-jev-v1 单次请求 expanded_input_tokens ≤ 32768,超出直接 422、整次判定作废。
"""
import json

import pytest

import app.typesafe_client as tc
from app import settings
from app.config import TypeSafeConfig
from app.core import Enzyme, Pattern
from app.typesafe_client import _estimate_tokens, judge_applicability

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")


def _enzymes(n, then_chars=3000, why_chars=300):
    return [Enzyme(id=f"e{i}", patterns=[Pattern(value="p")],
                   then="t" * then_chars, because="b" * why_chars)
            for i in range(n)]


def _capture(monkeypatch):
    """替身 system_one:记下实际发出的请求,按收到的题目逐个给出合法答案。"""
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["state"] = state
        captured["questions"] = questions
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                answers[key] = {"noul": 0.5}
            else:
                answers[key] = {"probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]}}
        return {"model": "jev-test", "answers": answers}

    monkeypatch.setattr(tc, "system_one", fake)
    return captured


def _rule_chars(e: Enzyme) -> int:
    """与判定实现同口径:逐条 noul 的 instructions.rule 的 JSON 长度。"""
    return len(json.dumps(tc._rule(e), ensure_ascii=False))


def _c_keys(questions: dict) -> list[str]:
    return [k for k in questions if k.startswith("c")]


@pytest.mark.asyncio
async def test_pool_trimmed_to_longest_affordable_prefix(monkeypatch):
    """36 条胖候选超预算:发出的是精排序最长前缀,p/q/pool 只含被留下的候选。"""
    captured = _capture(monkeypatch)
    es = _enzymes(36)
    app = await judge_applicability("x" * 3000, es, TS)

    assert 0 < len(_c_keys(captured["questions"])) < 36
    k = len(_c_keys(captured["questions"]))
    assert _c_keys(captured["questions"]) == [f"c{i}" for i in range(k)]      # 前缀,顺序不变
    assert set(captured["questions"]["best"]["criteria"]) == {f"c{i}" for i in range(k)} | {"none"}

    budget = settings.get("judge.token_budget")
    state_chars = len(captured["state"])
    R = sum(_rule_chars(e) for e in es[:k])
    assert _estimate_tokens(k, state_chars, R) <= budget
    # 前缀最长:再留一条就超预算
    assert _estimate_tokens(k + 1, state_chars, R + _rule_chars(es[k])) > budget

    assert list(app.p) == list(app.q) == [e.id for e in es[:k]]
    assert [row[0] for row in app.pool] == [e.id for e in es[:k]]


@pytest.mark.asyncio
async def test_sent_fields_are_truncated(monkeypatch):
    captured = _capture(monkeypatch)
    e = Enzyme(id="e0", patterns=[Pattern(value="p")], then="t" * 3000, because="b" * 2000)
    app = await judge_applicability("x" * 3000, [e], TS)

    rule = captured["questions"]["c0"]["instructions"]["rule"]              # 默认 v1 键
    assert len(rule["建议"]) == 400 and len(rule["依据"]) == 150
    assert captured["state"] == "x" * 1500
    assert app.request["state"] == "x" * 1500                               # 记录 = 实际发出
    assert e.then == "t" * 3000 and e.because == "b" * 2000                 # 存库原文不动


@pytest.mark.asyncio
async def test_zero_budget_disables_trimming(monkeypatch):
    monkeypatch.setenv("CE_JEV_TOKEN_BUDGET", "0")
    captured = _capture(monkeypatch)
    es = _enzymes(36)
    app = await judge_applicability("x" * 3000, es, TS)
    assert _c_keys(captured["questions"]) == [f"c{i}" for i in range(36)]
    assert len(app.p) == 36


@pytest.mark.asyncio
async def test_analogies_yield_to_candidates(monkeypatch):
    """候选把预算吃满时类比题整条不上(类比先丢);候选之后的剩余预算再按序收类比。"""
    captured = _capture(monkeypatch)
    fat = _enzymes(36)
    for e in fat[:3]:
        e._hint = "h" * 800
    app = await judge_applicability("x" * 3000, fat, TS, analogies=fat[:3])
    assert _c_keys(captured["questions"])                                  # 候选照发
    assert "a0" not in captured["questions"] and "same" not in captured["questions"]
    assert app.analog_q == {}

    captured.clear()
    small = _enzymes(3, then_chars=100, why_chars=50)
    hinted = [Enzyme(id=f"h{k}", patterns=[Pattern(value="p")], then="t") for k in range(5)]
    for e in hinted:
        e._hint = "h" * 800
    app = await judge_applicability("ctx", small, TS, analogies=hinted)
    assert _c_keys(captured["questions"]) == ["c0", "c1", "c2"]            # 候选一条不丢
    a_keys = [k for k in captured["questions"] if k.startswith("a")]
    assert 0 < len(a_keys) < 5 and a_keys == [f"a{k}" for k in range(len(a_keys))]
    assert set(app.analog_q) == {f"h{k}" for k in range(len(a_keys))}


@pytest.mark.asyncio
async def test_at_least_one_candidate_even_when_alone_over_budget(monkeypatch):
    """预算再小也至少发 1 条候选(精排第一名永远判到)。"""
    monkeypatch.setenv("CE_JEV_TOKEN_BUDGET", "500")
    captured = _capture(monkeypatch)
    es = _enzymes(3)
    app = await judge_applicability("x" * 3000, es, TS)
    assert _c_keys(captured["questions"]) == ["c0"]
    assert list(app.p) == ["e0"]
