"""judge.llm_fallback — Jev 失败后顺序接手的大模型判定。

Jev 正常时一次都不多调;Jev 失败/超时才在此时调大模型(融合任务已在跑就复用它),
按 judge.llm_fallback_p / judge.llm_fallback_max 注入,上限 judge.llm_fallback_timeout。
"""
import asyncio
import time

import pytest

from app.config import LLMConfig, TypeSafeConfig
from app.core import Enzyme, Pattern, Severity
from app.matching import pipeline as pl
from app.typesafe_client import Applicability

pytestmark = pytest.mark.asyncio

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")
LLM = LLMConfig(api_key="k", api_url="http://localhost:1")


class FakePG:
    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzyme(eid, **kw):
    # 模式与 context 不相干 → 低分非危急;severity 可覆盖
    return Enzyme(id=eid, patterns=[Pattern(value="zzz")], then=f"t-{eid}", **kw)


def _crit(eid):      # 危急级:无条件下放行(与 Jev 路径一致)
    return _enzyme(eid, severity=Severity.CRITICAL)


def _hot(eid):       # 高分普通记忆:模式全中 + 满 confidence/precision → 打分层 critical,severity 非危急
    return Enzyme(id=eid, patterns=[Pattern(value="浮点")], then=f"t-{eid}",
                  confidence=1.0, precision=1.0)


def _fat(eid):       # 超大 then → fit_pool 必须把池截短
    return Enzyme(id=eid, patterns=[Pattern(value="zzz")], then="r" * 3000)


def _app(enzymes) -> Applicability:
    return Applicability(p={e.id: 0.9 for e in enzymes},
                         q={e.id: 0.1 for e in enzymes},
                         none=0.0, model="jev")


async def _none_judge(*a, **k):
    return None


async def _run(monkeypatch, jev, llm, es, typesafe_config=TS, judge_llm_config=None, **kw):
    async def fake_recall(*a, **k):
        return es

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)
    monkeypatch.setattr(pl, "judge_applicability", jev)
    if llm is not None:
        monkeypatch.setattr(pl, "judge_llm", llm)
    trace: dict = {}
    res = await pl.run_pipeline(context="浮点数比较", search_query="浮点", pg=FakePG(),
                                qdrant=None, embedding_config=None, max_results=6,
                                trace=trace, typesafe_config=typesafe_config,
                                judge_llm_config=judge_llm_config, **kw)
    return res, trace


async def test_llm_never_called_when_jev_ok(monkeypatch):
    """Jev 成功且融合未开 → 接手 LLM 一次都不调(顺序兜底,不是并行备用)。"""
    called = []

    async def jev(context, enzymes, cfg, timeout=0, analogies=None):
        return _app(enzymes)

    async def llm(*a, **k):
        called.append(1)
        return {"e0": 0.99}

    res, trace = await _run(monkeypatch, jev, llm, [_enzyme("e0"), _enzyme("e1")],
                            judge_llm_config=LLM)
    assert called == []
    assert res and trace["jev_status"] == "ok"
    assert "llm_judge_status" not in trace and "judge" not in trace


async def test_llm_called_after_jev_fails_same_pool(monkeypatch):
    """Jev 快速失败 → LLM 在其后恰好被调一次,判的是同一个 fit_pool 截后的池。"""
    seen = []

    async def jev(context, enzymes, cfg, timeout=0, analogies=None):
        seen.append(("jev", [e.id for e in enzymes]))
        return None

    async def llm(context, enzymes, cfg, timeout=0):
        seen.append(("llm", [e.id for e in enzymes]))
        return {}

    res, trace = await _run(monkeypatch, jev, llm, [_fat(f"e{i}") for i in range(36)],
                            judge_llm_config=LLM)
    assert [k for k, _ in seen] == ["jev", "llm"]          # LLM 在 Jev 之后才起跑
    assert seen[0][1] == seen[1][1]                        # 同一个截断后的池
    assert 0 < len(seen[0][1]) < 36                        # 确实被 fit_pool 截断
    assert res == []                                       # llm_p 全空 → 无注入
    assert trace["llm_judge_status"] == "ok" and trace["judge"] == "llm_fallback"


async def test_llm_takeover_injects_top2(monkeypatch):
    """Jev 失败 → 大模型接手:取 p≥0.8 的前 2 条,reason=llm_fallback。"""
    async def llm(context, enzymes, cfg, timeout=0):
        return {"e0": 0.95, "e1": 0.85, "e2": 0.81, "e3": 0.5}

    res, trace = await _run(monkeypatch, _none_judge, llm,
                            [_enzyme(f"e{i}") for i in range(4)], judge_llm_config=LLM)
    assert [r.enzyme.id for r in res] == ["e0", "e1"]      # 降序前 2,0.81 被 max=2 截掉
    assert all(r.reason == "llm_fallback" for r in res)
    assert res[0].applicability == 0.95
    assert trace["jev_status"] == "failed"
    assert trace["llm_judge_status"] == "ok"
    assert trace["judge"] == "llm_fallback"
    assert dict(trace["llm_p"])["e0"] == 0.95
    assert trace["dosed_ids"] == ["e0", "e1"]


async def test_llm_takeover_below_threshold_injects_nothing(monkeypatch):
    async def llm(context, enzymes, cfg, timeout=0):
        return {"e0": 0.6, "e1": 0.5, "e2": 0.4, "e3": 0.79}

    res, trace = await _run(monkeypatch, _none_judge, llm,
                            [_enzyme(f"e{i}") for i in range(4)], judge_llm_config=LLM)
    assert res == [] and trace["dosed_ids"] == []


async def test_llm_takeover_still_passes_criticals(monkeypatch):
    async def llm(context, enzymes, cfg, timeout=0):
        return {"e1": 0.2}

    res, trace = await _run(monkeypatch, _none_judge, llm, [_crit("crit"), _enzyme("e1")],
                            judge_llm_config=LLM)
    assert [r.enzyme.id for r in res] == ["crit"]
    assert res[0].reason == "llm_fallback"
    assert res[0].applicability is None          # 危急项没被判到就不写概率


async def test_llm_takeover_tier_critical_is_not_passed_unjudged(monkeypatch):
    """分数>0.7 的 critical 层 ≠ severity 危急:和普通候选一样按 p/上限过滤。"""
    async def llm_low(context, enzymes, cfg, timeout=0):
        return {"hot": 0.5}

    res, trace = await _run(monkeypatch, _none_judge, llm_low, [_hot("hot")],
                            judge_llm_config=LLM)
    tiers = {eid: t for eid, s, t in trace["scored"]}
    assert tiers["hot"] == "critical"                     # 确实靠分数进的 critical 层
    assert res == []                                       # 但低 p 照样滤掉

    async def llm_high(context, enzymes, cfg, timeout=0):
        return {"hot": 0.9}

    res2, _ = await _run(monkeypatch, _none_judge, llm_high, [_hot("hot")],
                         judge_llm_config=LLM)
    assert [r.enzyme.id for r in res2] == ["hot"]          # p 达标才注入
    assert res2[0].reason == "llm_fallback" and res2[0].applicability == 0.9


async def test_llm_called_after_jev_timeout(monkeypatch):
    """Jev 超时后 LLM 才接手;总耗时 ≈ jev 超时 + LLM 耗时。"""
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.2")
    order = []

    async def slow_jev(context, enzymes, cfg, timeout=0, analogies=None):
        order.append("jev")
        await asyncio.sleep(5)

    async def llm(context, enzymes, cfg, timeout=0):
        order.append("llm")
        await asyncio.sleep(0.15)
        return {"e0": 0.95}

    t0 = time.perf_counter()
    res, trace = await _run(monkeypatch, slow_jev, llm, [_enzyme("e0")],
                            judge_llm_config=LLM)
    elapsed = time.perf_counter() - t0
    assert order == ["jev", "llm"]
    assert [r.enzyme.id for r in res] == ["e0"]
    assert res[0].reason == "llm_fallback"
    assert trace["jev_status"] == "timeout" and trace["llm_judge_status"] == "ok"
    assert 0.3 < elapsed < 1.0                              # ≈ 0.2 jev + 0.15 llm


async def test_llm_fallback_timeout_falls_back(monkeypatch):
    """大模型接手也超 judge.llm_fallback_timeout → _apply_fallback(危急放行,其余不注入)。"""
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.2")
    monkeypatch.setenv("CE_JUDGE_LLM_FALLBACK_TIMEOUT", "0.2")

    async def slow_jev(context, enzymes, cfg, timeout=0, analogies=None):
        await asyncio.sleep(5)

    async def slow_llm(context, enzymes, cfg, timeout=0):
        await asyncio.sleep(5)

    t0 = time.perf_counter()
    res, trace = await _run(monkeypatch, slow_jev, slow_llm, [_crit("crit"), _enzyme("e1")],
                            judge_llm_config=LLM)
    elapsed = time.perf_counter() - t0
    assert [r.enzyme.id for r in res] == ["crit"]
    assert res[0].reason == "fallback"
    assert trace["llm_judge_status"] == "timeout"
    assert "judge" not in trace
    assert elapsed < 1.0                                    # ≈ 0.2+0.2,绝不是 llm_timeout(8s)


async def test_fusion_task_reused_when_jev_fails(monkeypatch):
    """融合开着(judge.llm=1)的 LLM 任务已在跑:Jev 失败就复用它,不发起第二次调用。"""
    monkeypatch.setenv("CE_JUDGE_LLM", "1")
    called = []

    async def llm(context, enzymes, cfg, timeout=0):
        called.append(1)
        return {"e0": 0.9}

    res, trace = await _run(monkeypatch, _none_judge, llm, [_enzyme("e0"), _enzyme("e1")],
                            judge_llm_config=LLM)
    assert called == [1]                                    # 只调用了一次
    assert [r.enzyme.id for r in res] == ["e0"]
    assert res[0].reason == "llm_fallback"
    assert trace["judge"] == "llm_fallback" and trace["llm_judge_status"] == "ok"


async def test_llm_fallback_disabled(monkeypatch):
    """CE_JUDGE_LLM_FALLBACK=0 → Jev 失败绝不调 LLM,走 _apply_fallback。"""
    monkeypatch.setenv("CE_JUDGE_LLM_FALLBACK", "0")
    called = []

    async def llm(*a, **k):
        called.append(1)
        return {"e0": 0.99}

    res, trace = await _run(monkeypatch, _none_judge, llm, [_enzyme("e0")],
                            judge_llm_config=LLM)
    assert called == []
    assert res == []
    assert "llm_judge_status" not in trace and "judge" not in trace


async def test_llm_fallback_policy_value():
    from app.distill.records import _policy
    assert _policy({"judge": "llm_fallback", "jev_status": "failed"})["judge"] == "llm_fallback"
    assert _policy({"jev_status": "ok"})["judge"] == "jev"
    assert _policy({"jev_status": "failed"})["judge"] == "fallback"
    assert _policy({"jev_status": "timeout"})["judge"] == "fallback"
    assert _policy({})["judge"] == "dosage"
