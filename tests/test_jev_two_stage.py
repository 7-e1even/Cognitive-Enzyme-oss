"""两段判定(judge.two_stage):先并发分批逐条扫(scan),再对 top-compare_k 联合判定。

scan_applicability 分批/预算/容错;judge_two_stage 的组合语义;pipeline 接线
(两段拿未截原池、大模型兜底仍拿 fit_pool 截后池);system_one 并发闸门与 429 退避。
"""
import asyncio
import json

import httpx
import pytest

import app.typesafe_client as tc
from app import settings
from app.config import LLMConfig, TypeSafeConfig
from app.core import Enzyme, Pattern
from app.matching import pipeline as pl
from app.typesafe_client import (Applicability, _estimate_tokens, judge_two_stage,
                                 scan_applicability)

pytestmark = pytest.mark.asyncio

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")
LLM = LLMConfig(api_key="k", api_url="http://localhost:1")


class FakePG:
    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzymes(n, then="rule"):
    return [Enzyme(id=f"e{i}", patterns=[Pattern(value="p")], then=f"{then}-{i}", because="b")
            for i in range(n)]


def _fat(i):
    return Enzyme(id=f"e{i}", patterns=[Pattern(value="p")], then="r" * 3000, because="b")


def _p_of(i):
    return round(0.05 + 0.07 * i, 2)


def _capture(monkeypatch):
    """替身 system_one:逐条 noul 按题面里的 then(形如 rule-{i})回不同概率,
    记下发出的每个 questions;choice 题回均匀分布。"""
    captured = []

    async def fake(state, questions, config, timeout=0):
        captured.append(questions)
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                body = (q["instructions"].get("rule") or {}).get("建议", "")
                answers[key] = {"noul": _p_of(int(body.rsplit("-", 1)[1])) if "-" in body else 0.5}
            else:
                answers[key] = {"probabilities": {k: 1 / len(q["criteria"]) for k in q["criteria"]}}
        return {"model": "jev-test", "answers": answers}

    monkeypatch.setattr(tc, "system_one", fake)
    return captured


def _est(state: str, questions: dict) -> float:
    R = sum(len(json.dumps(q["instructions"]["rule"], ensure_ascii=False))
            for q in questions.values() if "rule" in q["instructions"])
    return _estimate_tokens(len(questions), len(state[:1500]), R)


async def test_scan_batches_and_merges(monkeypatch):
    """12 条、batch 3 → 4 个请求,每个 ≤3 道且只有 noul(无 best);p 按 id 正确合并。"""
    monkeypatch.setenv("CE_JEV_SCAN_BATCH", "3")
    captured = _capture(monkeypatch)
    p = await scan_applicability("ctx", _enzymes(12), TS)
    assert len(captured) == 4
    for qs in captured:
        assert 1 <= len(qs) <= 3
        assert "best" not in qs and all(q["type"] == "noul" for q in qs.values())
    assert p == {f"e{i}": _p_of(i) for i in range(12)}


async def test_scan_chunk_closes_on_budget(monkeypatch):
    """胖规则 + 小预算:批提前封口,每个请求的估算 ≤ 预算;每批至少 1 条。"""
    monkeypatch.setenv("CE_JEV_SCAN_BATCH", "12")
    monkeypatch.setenv("CE_JEV_TOKEN_BUDGET", "2500")   # 塞得下 1 条胖规则、塞不下 2 条
    captured = _capture(monkeypatch)
    p = await scan_applicability("ctx", [_fat(i) for i in range(6)], TS)
    assert len(captured) > 1
    for qs in captured:
        assert len(qs) >= 1 and _est("ctx", qs) <= 2500
    assert len(p) == 6


async def test_scan_partial_failure(monkeypatch):
    """一批失败只丢那批;全部失败 → None。"""
    monkeypatch.setenv("CE_JEV_SCAN_BATCH", "3")

    async def flaky(state, questions, config, timeout=0):
        if any("rule-4" in json.dumps(q["instructions"], ensure_ascii=False)
               for q in questions.values()):
            return None                                   # 含 e4 的那批整批失败
        return {"answers": {k: {"noul": 0.5} for k in questions}}

    monkeypatch.setattr(tc, "system_one", flaky)
    p = await scan_applicability("ctx", _enzymes(9), TS)
    assert set(p) == {"e0", "e1", "e2", "e6", "e7", "e8"}   # e3–e5 同批,整批丢掉

    async def dead(state, questions, config, timeout=0):
        return None

    monkeypatch.setattr(tc, "system_one", dead)
    assert await scan_applicability("ctx", _enzymes(6), TS) is None


async def test_two_stage_flow(monkeypatch):
    """第二阶段拿 scan 概率最高的 compare_k 条(降序);p 用第一阶段全部结果,
    q/none 来自第二阶段;mode/calls/pool/scan 齐。"""
    monkeypatch.setenv("CE_JEV_SCAN_POOL", "8")
    monkeypatch.setenv("CE_JEV_SCAN_BATCH", "8")
    monkeypatch.setenv("CE_JEV_COMPARE_K", "3")
    monkeypatch.setattr(tc, "fit_pool", lambda context, es: es[:2])
    captured = _capture(monkeypatch)
    app = await judge_two_stage("ctx", _enzymes(10), TS)

    assert app is not None and len(captured) == 3            # 1 扫 + incumbent 比 + supplemental 比
    stage2 = captured[1]
    assert "best" in stage2
    assert set(stage2) == {"best"}                         # 不重复询问扫描 p
    assert [v["建议"] for k,v in stage2["best"]["criteria"].items() if k != "none"] == [
        "rule-0", "rule-1"]                               # incumbent 独立保护
    assert set(stage2["best"]["criteria"]) == {"c0", "c1", "none"}
    assert [v["建议"] for k,v in captured[2]["best"]["criteria"].items() if k != "none"] == [
        "rule-7", "rule-6", "rule-5"]
    assert app.mode == "two_stage" and app.calls == 3
    assert app.scan == app.p == {f"e{i}": _p_of(i) for i in range(8)}
    assert set(app.q) == {"e0", "e1"} and set(app.supplement_q) == {"e7", "e6", "e5"} and app.none == pytest.approx(1/4)
    assert app.pool == [[f"e{i}", 1] for i in range(8)]      # 全部扫视池,不是 top-3


async def test_two_stage_scans_analogies_beyond_pool(monkeypatch):
    """类比候选在 scan_pool 之外也补进扫视池(它们得有 p 才可能入选)。"""
    monkeypatch.setenv("CE_JEV_SCAN_POOL", "4")
    monkeypatch.setenv("CE_JEV_SCAN_BATCH", "8")
    monkeypatch.setattr(tc, "fit_pool", lambda context, es: es[:2])
    _capture(monkeypatch)
    hx = Enzyme(id="hx", patterns=[Pattern(value="p")], then="rule-9", because="b")
    hx._hint = "过去那次适用情境"
    app = await judge_two_stage("ctx", _enzymes(8), TS, analogies=[hx])
    assert app is not None
    assert set(app.scan) == {"e0", "e1", "e2", "e3", "hx"}
    assert app.scan["hx"] == _p_of(9)
    assert ["hx", 1] in app.pool


async def test_two_stage_stage2_failure(monkeypatch):
    """第一阶段成功、联合判定失败 → 整个判定 None。"""
    async def fake(state, questions, config, timeout=0):
        if "best" in questions:
            return None
        return {"answers": {k: {"noul": 0.5} for k in questions}}

    monkeypatch.setattr(tc, "system_one", fake)
    assert await judge_two_stage("ctx", _enzymes(6), TS) is None


async def test_choice_only_budget_preserves_original_prefix_and_scan_keys(monkeypatch):
    from app.distill.export import _cand_rules
    monkeypatch.setitem(settings._values, "judge.token_budget", 8000)
    es = [Enzyme(id=f"e{i}", patterns=[Pattern(value="p"*150)], then="r"*300, because="b"*100)
          for i in range(10)]
    captured = []
    async def fake(state, questions, config, timeout=0):
        captured.append(questions)
        options = questions["best"]["criteria"]
        return {"answers": {"best": {"probabilities": {k:int(k[1:])/20 for k in options if k!="none"} | {"none": .1}}}}
    monkeypatch.setattr(tc, "system_one", fake)
    app = await tc._compare_scanned("x"*1024, [es[1],es[5],es[7],es[0]], es,
                                   {e.id:.5 for e in es}, TS, 6, [])
    assert set(app.q) == {"e1", "e5", "e7"}
    assert app.q["e5"] == .25
    qs = captured[0]
    assert set(qs) == {"best"}
    assert set(qs["best"]["criteria"]) == {"c1", "c5", "c7", "none"}
    assert _estimate_tokens(1, 1024, len(json.dumps(qs["best"]["criteria"],ensure_ascii=False))) <= 8000
    rules = _cand_rules({"request":app.request,"candidates":[{"key":"c5"}]})
    assert "r"*300 in rules["c5"]


async def test_two_stage_keeps_original_candidates_beyond_scan_prefix(monkeypatch):
    monkeypatch.setitem(settings._values, "judge.scan_pool", 2)
    monkeypatch.setitem(settings._values, "judge.token_budget", 0)
    _capture(monkeypatch)
    es = _enzymes(6)
    app = await judge_two_stage("ctx", es, TS)
    assert set(app.p) == {e.id for e in es}
    assert set(app.q) == {e.id for e in es}


# ── pipeline 接线 ────────────────────────────────────────────────

async def _run_pipeline(monkeypatch, jev, llm, es, typesafe_config=TS, judge_llm_config=None):
    async def fake_recall(*a, **k):
        return es

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)
    if jev is not None:
        monkeypatch.setattr(pl, "judge_applicability", jev)
    if llm is not None:
        monkeypatch.setattr(pl, "judge_llm", llm)
    trace: dict = {}
    res = await pl.run_pipeline(context="浮点数比较", search_query="浮点", pg=FakePG(),
                                qdrant=None, embedding_config=None, max_results=6,
                                trace=trace, typesafe_config=typesafe_config,
                                judge_llm_config=judge_llm_config)
    return res, trace


async def test_pipeline_two_stage_gets_unfitted_pool(monkeypatch):
    """two_stage 开:Jev 任务拿未截原池;Jev 失败仍顺序接手,接手的大模型拿 fit_pool 截后池。"""
    monkeypatch.setenv("CE_JEV_TWO_STAGE", "1")
    got = {}

    async def jev2(context, enzymes, cfg, timeout=0, analogies=None):
        got["jev_pool"] = [e.id for e in enzymes]
        return None                                       # 两段判定失败 → 接手路径

    async def jev1(*a, **k):
        got["single_called"] = True
        return None

    async def llm(context, enzymes, cfg, timeout=0):
        got["llm_pool"] = [e.id for e in enzymes]
        return {"e0": 0.95}

    monkeypatch.setattr(pl, "judge_two_stage", jev2)
    res, trace = await _run_pipeline(monkeypatch, jev1, llm,
                                   [_fat(i) for i in range(36)], judge_llm_config=LLM)
    assert "single_called" not in got
    assert len(got["jev_pool"]) == 36                      # 原池,未被 fit_pool 截
    assert 0 < len(got["llm_pool"]) < 36                   # 兜底判的还是截后池
    assert [r.enzyme.id for r in res] == ["e0"]            # 接手注入
    assert res[0].reason == "llm_fallback"


async def test_pipeline_two_stage_off_uses_fitted_pool(monkeypatch):
    """two_stage 关:照旧 judge_applicability + fit_pool 截后池。"""
    got = {}

    async def jev1(context, enzymes, cfg, timeout=0, analogies=None):
        got["pool"] = [e.id for e in enzymes]
        return None

    async def jev2(*a, **k):
        got["two_stage_called"] = True
        return None

    monkeypatch.setattr(pl, "judge_two_stage", jev2)
    await _run_pipeline(monkeypatch, jev1, None, [_fat(i) for i in range(36)])
    assert "two_stage_called" not in got
    assert 0 < len(got["pool"]) < 36


async def test_pipeline_two_stage_trace_and_policy(monkeypatch):
    """两段判定成功:trace 带 jev_mode/jev_calls,判定记录 policy 原样携带。"""
    monkeypatch.setenv("CE_JEV_TWO_STAGE", "1")

    async def jev2(context, enzymes, cfg, timeout=0, analogies=None):
        return Applicability(p={e.id: 0.9 for e in enzymes}, q={e.id: 0.2 for e in enzymes},
                             none=0.0, model="jev", mode="two_stage", calls=4)

    monkeypatch.setattr(pl, "judge_two_stage", jev2)
    res, trace = await _run_pipeline(monkeypatch, None, None,
                                   [Enzyme(id="e0", patterns=[Pattern(value="浮点")], then="t")])
    assert trace["jev_mode"] == "two_stage" and trace["jev_calls"] == 4

    from app.distill.records import _policy
    pol = _policy(trace)
    assert pol["judge"] == "jev" and pol["jev_mode"] == "two_stage" and pol["jev_calls"] == 4


# ── system_one 并发闸门 / 429 退避 ───────────────────────────────

Q = {"q": {"type": "noul", "instructions": "x", "criteria": {"true": "a", "false": "b"}}}


def _resp(status, payload=None):
    return httpx.Response(status, json=payload or {"answers": {"q": {"noul": 0.7}}},
                          request=httpx.Request("POST", "https://ts.invalid/v1/systemone"))


def _fake_client(monkeypatch, post):
    class C:
        async def post(self, *a, **k):
            return await post(*a, **k)

    monkeypatch.setattr(tc, "_get_client", lambda: C())
    monkeypatch.setattr(tc, "_breaker", {})


async def test_system_one_concurrency_gate(monkeypatch):
    """judge.concurrency=2:8 个并发调用,同时在飞的请求 ≤ 2。"""
    monkeypatch.setenv("CE_TYPESAFE_CONCURRENCY", "2")
    inflight = peak = 0

    async def post(*a, **k):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.03)
        inflight -= 1
        return _resp(200)

    _fake_client(monkeypatch, post)
    out = await asyncio.gather(*(tc.system_one("s", dict(Q), TS) for _ in range(8)))
    assert all(o is not None for o in out)
    assert peak <= 2


async def test_system_one_429_retry(monkeypatch):
    """429 退避重试:429 → 200 成功;一直 429 至多 3 次后返回 None,且不触发熔断。"""
    monkeypatch.setattr(tc, "_429_BACKOFF", (0.01, 0.01))
    calls = 0

    async def post(*a, **k):
        nonlocal calls
        calls += 1
        return _resp(429) if calls == 1 else _resp(200)

    _fake_client(monkeypatch, post)
    data = await tc.system_one("s", dict(Q), TS)
    assert data["answers"]["q"]["noul"] == 0.7 and calls == 2

    calls = 0

    async def busy(*a, **k):
        nonlocal calls
        calls += 1
        return _resp(429)

    _fake_client(monkeypatch, busy)
    assert await tc.system_one("s", dict(Q), TS) is None
    assert calls == 3                                       # 首发 + 2 次重试
    assert tc._breaker == {}                                # 429 不进熔断


async def test_system_one_gate_per_event_loop(monkeypatch):
    """每个 event loop 各一把信号量:换 loop 不复用旧原语(不报 bound to a different loop)。"""
    monkeypatch.setenv("CE_TYPESAFE_CONCURRENCY", "1")

    async def post(*a, **k):
        return _resp(200)

    _fake_client(monkeypatch, post)
    assert await tc.system_one("s", dict(Q), TS) is not None   # 当前 loop 建了一把

    result = []

    def other_loop():
        result.append(asyncio.run(tc.system_one("s", dict(Q), TS)))

    t = asyncio.get_running_loop().run_in_executor(None, other_loop)
    await t
    assert result[0] is not None
