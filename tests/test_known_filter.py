"""已会过滤(known_filter)单测 — 纯 mock,不连库不调真模型。

覆盖:Engine.harvest 的已会门(开启拦/不拦、默认不调 LLM、fail-open)、judge 的显式原因、
enzyme_trigger 重建触发情境、白日梦/交叉/突变三条新产线的门、回填脚本的 apply/rollback。
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import settings  # noqa: E402
from app.core import Enzyme, Pattern  # noqa: E402
from app.core.engine import Engine  # noqa: E402
from app.learning import crossover, dream, known_filter, mutation  # noqa: E402
from app.learning.quality_gate import QualityResult, REJECT  # noqa: E402
from scripts import known_backfill as kb  # noqa: E402


# ── Fakes / 打桩 ──────────────────────────────────────────────

def _engine_stub():
    """最小 harvest 替身:只走得到 dedup / 质量门之前的那几行。"""
    eng = Engine.__new__(Engine)
    eng.config = SimpleNamespace(embedding=None, typesafe=None)
    eng.qdrant = SimpleNamespace(search=lambda *a, **k: None)
    eng.pg = SimpleNamespace()
    return eng


def _patch_embed_none(monkeypatch):
    async def fake_embed(text, config=None):
        return None
    monkeypatch.setattr("app.llm_client.embed", fake_embed)


def _patch_quality(monkeypatch):
    """质量门打桩:记录调用并回 REJECT(走到就说明已会过滤没拦)。"""
    calls = []

    async def fake_assess(insight, context, similar_enzymes=None, typesafe_config=None):
        calls.append((insight, context))
        return QualityResult(decision=REJECT, score=1.0, reason="测试拒绝")

    monkeypatch.setattr("app.learning.quality_gate.assess_quality", fake_assess)
    return calls


def _patch_known(monkeypatch, answer="闭卷回答", verdict=None, boom=False):
    """打桩 known_filter 的闭卷作答与评审;boom=True 表示不该被调用。"""
    async def fake_chat(system, user, config=None, temperature=None, timeout=30.0):
        assert not boom, "已会过滤不该被调用"
        return answer

    async def fake_chat_json(system, user, config=None, timeout=30.0):
        assert not boom, "已会过滤不该被调用"
        return verdict

    monkeypatch.setattr(known_filter, "chat", fake_chat)
    monkeypatch.setattr(known_filter, "chat_json", fake_chat_json)


class _FakePG:
    def __init__(self, enzymes=None):
        self.enzymes = {e.id: e for e in (enzymes or [])}
        self.upserted = []
        self.events = []

    async def upsert_enzyme(self, e, expected_version=None):
        self.enzymes[e.id] = e
        self.upserted.append(e.id)
        return 1

    async def upsert_relation(self, rel):
        pass

    async def log_event(self, event):
        self.events.append(event)

    async def list_enzymes(self, **kw):
        return list(self.enzymes.values())


class _FakeSync:
    def __init__(self):
        self.synced = []

    async def sync_enzyme(self, e, generation=None):
        self.synced.append(e.id)


def _enzyme(**kw):
    kw.setdefault("patterns", [Pattern(value="p1")])
    kw.setdefault("then", "做点什么")
    return Enzyme(**kw)


# ── Engine.harvest 的已会门 ────────────────────────────────────

async def test_harvest_known_covered(monkeypatch):
    """开启 + 闭卷已涵盖 → status="known",reason 透传,质量门不再跑。"""
    eng = _engine_stub()
    _patch_embed_none(monkeypatch)
    calls = _patch_quality(monkeypatch)
    _patch_known(monkeypatch, verdict={"covered": True, "reason": "常识"})
    with settings.override({"learning.skip_known": True}):
        res = await eng.harvest(insight="先解一个更简单的同类问题", context="遇到复杂问题没有思路")
    assert res["status"] == "known" and res["reason"] == "常识", res
    assert calls == []


async def test_harvest_known_not_covered(monkeypatch):
    """开启 + 闭卷没涵盖 → 照常往下走(进质量门)。"""
    eng = _engine_stub()
    _patch_embed_none(monkeypatch)
    calls = _patch_quality(monkeypatch)
    _patch_known(monkeypatch, verdict={"covered": False, "reason": "有特定的坑"})
    with settings.override({"learning.skip_known": True}):
        res = await eng.harvest(insight="某个具体的坑", context="调试 segfault")
    assert res["status"] == "rejected", res
    assert len(calls) == 1


async def test_harvest_known_off_by_default(monkeypatch):
    """默认关闭:闭卷作答/评审都不该被调用。"""
    eng = _engine_stub()
    _patch_embed_none(monkeypatch)
    calls = _patch_quality(monkeypatch)
    _patch_known(monkeypatch, boom=True)
    res = await eng.harvest(insight="x", context="y")
    assert res["status"] == "rejected", res
    assert len(calls) == 1


async def test_harvest_known_fail_open(monkeypatch):
    """开启但闭卷作答失败(answer=None)→ 不拦,进质量门。"""
    eng = _engine_stub()
    _patch_embed_none(monkeypatch)
    calls = _patch_quality(monkeypatch)
    _patch_known(monkeypatch, answer=None)
    with settings.override({"learning.skip_known": True}):
        res = await eng.harvest(insight="x", context="y")
    assert res["status"] == "rejected", res
    assert len(calls) == 1


async def test_harvest_known_empty_context(monkeypatch):
    """开启但 context 为空 → judge 判 no-trigger 不拦,且不调 LLM。"""
    eng = _engine_stub()
    _patch_embed_none(monkeypatch)
    calls = _patch_quality(monkeypatch)
    _patch_known(monkeypatch, boom=True)
    with settings.override({"learning.skip_known": True}):
        res = await eng.harvest(insight="x", context="")
    assert res["status"] == "rejected", res
    assert len(calls) == 1


# ── judge 的显式原因 ──────────────────────────────────────────

async def test_judge_reason_passthrough(monkeypatch):
    _patch_known(monkeypatch, verdict={"covered": True, "reason": "就是常识"})
    assert await known_filter.judge({"trigger": "t", "then": "x"}) == (True, "就是常识")


async def test_judge_reason_error(monkeypatch):
    async def boom(system, user, config=None, temperature=None, timeout=30.0):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(known_filter, "chat", boom)
    assert await known_filter.judge({"trigger": "t", "then": "x"}) == (False, "error")


async def test_judge_reason_bad_verdict(monkeypatch):
    _patch_known(monkeypatch, verdict=["不是对象"])
    assert await known_filter.judge({"trigger": "t", "then": "x"}) == (False, "bad-verdict")


async def test_judge_reason_no_trigger_and_no_answer(monkeypatch):
    assert await known_filter.judge({"trigger": "", "then": "x"}) == (False, "no-trigger")
    assert await known_filter.judge({"trigger": "t", "then": ""}) == (False, "no-trigger")
    _patch_known(monkeypatch, answer=None)
    assert await known_filter.judge({"trigger": "t", "then": "x"}) == (False, "no-answer")


# ── enzyme_trigger:重建已有酶的触发情境 ─────────────────────────

def test_enzyme_trigger_clause_colon():
    e = _enzyme(then="遇到复杂问题没有思路时：先解一个更简单的同类问题",
                exemplars=["后来的样本情境"], because="从实际任务中发现: 别的现场")
    assert known_filter.enzyme_trigger(e) == "遇到复杂问题没有思路时"


def test_enzyme_trigger_clause_comma():
    e = _enzyme(then="处理多条件约束时，拆开条件逐条看")
    assert known_filter.enzyme_trigger(e) == "处理多条件约束时"


def test_enzyme_trigger_clause_skips_adverb():
    """「同时，」这类副词不是情境从句收尾,不能当切点。"""
    e = _enzyme(then="同时，在处理报错时：先读完整堆栈")
    assert known_filter.enzyme_trigger(e) == "同时，在处理报错时"


def test_enzyme_trigger_clause_skips_duration_words():
    """「耗时/历时/费时」是时长不是情境,不当切点 → 落到下一优先级。"""
    e = _enzyme(then="统计各环节耗时，再定位瓶颈", because="从实际任务中发现: 性能分析现场")
    assert known_filter.enzyme_trigger(e) == "性能分析现场"


def test_enzyme_trigger_patch_head_skips_clause():
    """补丁式 then(开头括注)不切从句 → 落到 because 前缀。"""
    e = _enzyme(then="（保留原 then 全文…）新增段", because="从实际任务中发现: 原现场",
                exemplars=["样本情境"])
    assert known_filter.enzyme_trigger(e) == "原现场"


def test_enzyme_trigger_because_beats_exemplar():
    """then 无从句时:because「从实际任务中发现:」优先于经验样本。"""
    e = _enzyme(then="直接给做法", because="从实际任务中发现: 调试 segfault 的现场",
                exemplars=["后学到的样本"])
    assert known_filter.enzyme_trigger(e) == "调试 segfault 的现场"


def test_enzyme_trigger_exemplar_fallback():
    e = _enzyme(then="直接给做法", exemplars=["诞生情境原文"], because="普通理由")
    assert known_filter.enzyme_trigger(e) == "诞生情境原文"


def test_enzyme_trigger_fallback_patterns():
    e = _enzyme(then="直接给做法", patterns=[Pattern(value="segfault"), Pattern(value="")],
                conditions=["c++ 项目"], because="")
    assert known_filter.enzyme_trigger(e) == "segfault；c++ 项目"


def test_enzyme_trigger_non_prefix_because_falls_back():
    e = _enzyme(then="直接给做法", patterns=[Pattern(value="p9")],
                because="（保留原 because 全文…）新增段")
    assert known_filter.enzyme_trigger(e) == "p9"


# ── 白日梦 / 交叉 / 突变 ──────────────────────────────────────

def _dream_fakes(monkeypatch, covered):
    a = _enzyme(then="A", domain="d1")
    b = _enzyme(then="B", domain="d2", patterns=[Pattern(value="p2")])

    async def pick(pg, count=2):
        return [(a, b)]

    async def combine(x, y, cfg):
        return {"insight": "组合洞察", "scenario": "", "reasoning": ""}

    monkeypatch.setattr(dream, "_pick_random_pairs", pick)
    monkeypatch.setattr(dream, "_llm_creative_combine", combine)
    _patch_known(monkeypatch, verdict={"covered": covered, "reason": "r"})
    return a, b


async def test_dream_known_skips_upsert(monkeypatch):
    _dream_fakes(monkeypatch, covered=True)
    pg, sync = _FakePG(), _FakeSync()
    with settings.override({"learning.skip_known": True}):
        res = await dream.run_dream_cycle(pg, None, sync, SimpleNamespace(available=True))
    assert res["dreams_known"] == 1 and res["dreams_created"] == 0, res
    assert pg.upserted == []


async def test_dream_not_known_upserts(monkeypatch):
    _dream_fakes(monkeypatch, covered=False)
    pg, sync = _FakePG(), _FakeSync()
    with settings.override({"learning.skip_known": True}):
        res = await dream.run_dream_cycle(pg, None, sync, SimpleNamespace(available=True))
    assert res["dreams_created"] == 1 and res["dreams_known"] == 0, res
    assert len(pg.upserted) == 1


async def test_crossover_known_returns_none(monkeypatch):
    a = _enzyme(then="A", domain="d")
    b = _enzyme(then="B", domain="d", patterns=[Pattern(value="p1"), Pattern(value="p2")])
    _patch_known(monkeypatch, verdict={"covered": True, "reason": "r"})
    pg, sync = _FakePG(), _FakeSync()
    with settings.override({"learning.skip_known": True}):
        child = await crossover.crossover(a, b, "conditions_merge", pg, sync)
    assert child is None and pg.upserted == []


async def test_mutation_skips_quarantined_parent():
    """非 active 的父酶不突变 —— 防止借道 mutation 复活被隔离的已会酶。"""
    pg, sync = _FakePG(), _FakeSync()
    parent = _enzyme(confidence=0.9, trigger_count=10, conditions=["c1"], status="quarantine")
    assert await mutation.mutate_enzyme(parent, "broaden", pg, sync) is None
    assert pg.upserted == []

    parent.status = "active"
    mutant = await mutation.mutate_enzyme(parent, "broaden", pg, sync)
    assert mutant is not None and pg.upserted == [mutant.id]


# ── 回填脚本:apply / rollback(假引擎,不碰真库)──────────────────

async def test_backfill_apply(tmp_path):
    e1 = _enzyme(status="active")
    e2 = _enzyme(status="active")
    report = tmp_path / "r.jsonl"
    report.write_text(
        json.dumps({"id": e1.id, "covered": True, "reason": "常识"}, ensure_ascii=False) + "\n"
        + json.dumps({"id": e2.id, "covered": False, "reason": "新颖"}, ensure_ascii=False) + "\n"
        + json.dumps({"id": "ghost", "covered": True, "reason": "x"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    eng = SimpleNamespace(pg=_FakePG([e1, e2]), updated=[],
                          get_enzyme=None, update_enzyme=None)

    async def get_enzyme(eid):
        return eng.pg.enzymes.get(eid)

    async def update_enzyme(e, expected_version=None):
        eng.updated.append(e.id)

    eng.get_enzyme, eng.update_enzyme = get_enzyme, update_enzyme
    counts = await kb.apply_report(eng, str(report))
    assert counts == {"quarantined": 1, "skipped": 1}, counts
    assert e1.status == "quarantine" and "known:closed_book" in e1.tags
    assert e2.status == "active" and e2.tags == []
    assert eng.pg.events[0].detail["action"] == "known_quarantine"


async def test_backfill_rollback():
    e1 = _enzyme(status="quarantine", tags=["known:closed_book", "other"])
    e2 = _enzyme(status="quarantine", tags=[])          # 不是我们隔离的 → 不动
    pg = _FakePG([e1, e2])
    eng = SimpleNamespace(pg=pg, updated=[])

    async def update_enzyme(e, expected_version=None):
        eng.updated.append(e.id)

    eng.update_enzyme = update_enzyme
    counts = await kb.rollback(eng)
    assert counts == {"restored": 1}, counts
    assert e1.status == "active" and e1.tags == ["other"]
    assert e2.status == "quarantine"
    assert pg.events[0].detail["action"] == "known_restore"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
