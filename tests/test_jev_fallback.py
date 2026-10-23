"""判官失灵回退(judge.fallback_budget):Jev 尝试过但失败/超时时,只放危急项与至多
budget 条非危急项(reason="fallback",不挑探索项);判官未启用时行为不变(固定剂量)。"""
import asyncio

import pytest

from app.config import TypeSafeConfig
from app.core import Enzyme, Origin, Pattern, Severity
from app.matching import pipeline as pl

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")


class FakePG:
    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzyme(eid, **kw):
    # 模式与 context 不相干 → 低分非危急;severity / origin / confidence 可覆盖
    return Enzyme(id=eid, patterns=[Pattern(value="zzz")], then=f"t-{eid}", **kw)


def _hot(eid):
    """高分普通记忆:模式全中 + 满 confidence/precision → 打分层 critical,severity 非危急。"""
    return Enzyme(id=eid, patterns=[Pattern(value="浮点")], then=f"t-{eid}",
                  confidence=1.0, precision=1.0)


async def _none_judge(*a, **k):
    return None


async def _run(monkeypatch, judge, es, rate=0.0, **kw):
    async def fake_recall(*a, **k):
        return es

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", rate)
    monkeypatch.setattr(pl, "judge_applicability", judge)
    trace: dict = {}
    res = await pl.run_pipeline(context="浮点数比较", search_query="浮点", pg=FakePG(),
                                qdrant=None, embedding_config=None, max_results=6,
                                trace=trace, **kw)
    return res, trace


@pytest.mark.asyncio
async def test_failed_judge_injects_nothing_by_default(monkeypatch):
    """judge.fallback_budget 默认 0:判官失败且没有危急项 → 什么都不注入。"""
    es = [_enzyme(f"e{i}") for i in range(5)]
    res, trace = await _run(monkeypatch, _none_judge, es, typesafe_config=TS)
    assert res == [] and trace["jev_status"] == "failed" and trace["dosed_ids"] == []


@pytest.mark.asyncio
async def test_failed_judge_keeps_critical_severity(monkeypatch):
    """判官失败:危急级记忆(severity=critical)照常放行(不压制真警报),非危急按预算(默认 0)不进。"""
    es = [_enzyme(f"e{i}") for i in range(3)]
    es.insert(1, _enzyme("crit", severity=Severity.CRITICAL))
    res, trace = await _run(monkeypatch, _none_judge, es, typesafe_config=TS)
    assert [r.enzyme.id for r in res] == ["crit"]
    assert res[0].reason == "fallback" and trace["dosed_ids"] == ["crit"]


@pytest.mark.asyncio
async def test_score_critical_without_severity_is_not_passed(monkeypatch):
    """仅因分数高(>0.7)落入 critical 层、severity 非危急的记忆不是"危急项"——
    判官失灵时仍受 fallback_budget 约束(默认 0 → 不注入)。"""
    res, trace = await _run(monkeypatch, _none_judge, [_enzyme("e0"), _hot("hot")],
                            typesafe_config=TS)
    scored = {eid: t for eid, s, t in trace["scored"]}
    assert scored["hot"] == "critical"                        # 确实是靠分数进的 critical 层
    assert res == [] and trace["jev_status"] == "failed"


@pytest.mark.asyncio
async def test_fallback_budget_caps_noncritical(monkeypatch):
    """fallback_budget=2:危急项 + 至多 2 条非危急,reason=fallback,不挑探索项。"""
    monkeypatch.setenv("CE_JEV_FALLBACK_BUDGET", "2")
    es = [_enzyme("crit", severity=Severity.CRITICAL)] + [_enzyme(f"e{i}") for i in range(5)]
    es.append(_enzyme("dream", origin=Origin.EVOLVED, confidence=0.2))   # 探索槽候选
    res, trace = await _run(monkeypatch, _none_judge, es, rate=1.0, typesafe_config=TS)
    ids = [r.enzyme.id for r in res]
    assert ids[0] == "crit" and len(res) == 3
    assert all(r.reason == "fallback" for r in res)
    assert "dream" not in ids and not any(r.exploratory for r in res)   # 探索槽必命中也不挑
    assert trace["dosed_ids"] == ids


@pytest.mark.asyncio
async def test_timed_out_judge_uses_fallback(monkeypatch):
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.05")

    async def slow(*a, **k):
        await asyncio.sleep(1)

    res, trace = await _run(monkeypatch, slow, [_enzyme("e0")], typesafe_config=TS)
    assert res == [] and trace["jev_status"] == "timeout" and trace["dosed_ids"] == []


@pytest.mark.asyncio
async def test_no_judge_keeps_fixed_dosage(monkeypatch):
    """判官未配置/未启用:行为不变——固定剂量 5,jev_status 不设置,判官不被调用。"""
    calls = []

    async def judge(*a, **k):
        calls.append(1)

    es = [_enzyme(f"e{i}") for i in range(8)]
    res, trace = await _run(monkeypatch, judge, es)                                # 未配置
    assert len(res) == 5 and all(r.reason == "dosage" for r in res)
    assert "jev_status" not in trace and calls == []

    res, trace = await _run(monkeypatch, judge, es, typesafe_config=TypeSafeConfig())  # 无 key
    assert len(res) == 5 and calls == []
