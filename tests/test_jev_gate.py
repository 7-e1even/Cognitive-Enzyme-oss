"""Stage 4.5 适用性判定(TypeSafe Jev)单测 — 纯内存,Jev 调用全部替身。

覆盖:主选 + 高 noul 补充、choice 判 none 时整体静默、critical 告警放行、
     冷启动解锁(低 confidence 高适用 → exploratory,每次至多一条)、
     探索槽只在"可能适用"的假设里挑、未配置回退原剂量、失败/超时保守回退、
     parallel 模式与 rerank 并行、响应缺字段容错。
"""
import asyncio

import pytest

from app.config import TypeSafeConfig
from app.core import Enzyme, Origin, Pattern, Severity
from app.matching import pipeline as pl
from app.matching.pipeline import MatchResult, _apply_jev_dosage
from app.typesafe_client import Applicability, judge_applicability

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")
MISS = lambda: 0.99   # noqa: E731  探索槽必不命中
FIRE = lambda: 0.0    # noqa: E731  探索槽必命中


def _mk(eid, conf=0.8, severity=Severity.INFO, origin=Origin.HARVESTED, score=0.5):
    e = Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}",
               severity=severity, confidence=conf, origin=origin)
    return MatchResult(enzyme=e, score=score, tier="relevant")


def _app(p, q=None, none=0.0):
    return Applicability(p=p, q=q or {}, none=none)


def test_primary_plus_supplement():
    rs = [_mk("a"), _mk("b"), _mk("c"), _mk("d")]
    app = _app(p={"a": 0.7, "b": 0.95, "c": 0.2, "d": 0.92},
               q={"a": 0.1, "b": 0.8, "c": 0.0, "d": 0.1})
    sel = _apply_jev_dosage(rs, app, max_results=6, rng=MISS)
    # 主选 = choice 最高 b;补充 = p≥0.9 且 q ≥ 10%·q_max 的 d;a 的 p 不够补充线
    assert [r.enzyme.id for r in sel] == ["b", "d"]
    assert sel[0].applicability == 0.95


def test_supplement_needs_relative_support():
    rs = [_mk("a"), _mk("b")]
    app = _app(p={"a": 0.95, "b": 0.95}, q={"a": 0.9, "b": 0.01})
    sel = _apply_jev_dosage(rs, app, max_results=6, rng=MISS)
    assert [r.enzyme.id for r in sel] == ["a"]  # b 的相对优势 < 10%,不补


def test_none_silences_everything():
    rs = [_mk("a"), _mk("b", severity=Severity.CRITICAL)]
    app = _app(p={"a": 0.95, "b": 0.95}, q={"a": 0.2, "b": 0.1}, none=0.7)
    assert _apply_jev_dosage(rs, app, max_results=6, rng=MISS) == []


def test_methodology_hint_bypasses_none_gate():
    """通用方法论(scope:general)可绕过 none 门;显式关闭时行为不变。"""
    from app import settings
    general = _mk("g")
    general.enzyme.tags = ["scope:general"]
    rs = [general, _mk("k"), _mk("weak")]
    rs[2].enzyme.tags = ["scope:general"]
    app = _app(p={"g": 0.45, "k": 0.45, "weak": 0.2}, q={"g": 0.1, "k": 0.1, "weak": 0.0}, none=0.7)
    with settings.override({"selection.hint_max": 0}):
        assert _apply_jev_dosage(rs, app, max_results=6, rng=MISS) == []
    with settings.override({"selection.hint_max": 2}):
        sel = _apply_jev_dosage(rs, app, max_results=6, rng=MISS)
    assert [r.enzyme.id for r in sel] == ["g"] and sel[0].reason == "hint"


def test_weak_primary_not_injected():
    rs = [_mk("a")]
    # choice 概率与 none 合计为 1:q=0.3 的单候选意味着 none=0.7 → 不推
    assert _apply_jev_dosage(rs, _app(p={"a": 0.9}, q={"a": 0.3}, none=0.7), 6, rng=MISS) == []
    # p 不够、choice 也没有高到可以单独拍板(<0.6)→ 不推;q≥0.6 的情形见 test_confident_choice_overrides_low_noul
    assert _apply_jev_dosage(rs, _app(p={"a": 0.2}, q={"a": 0.55}, none=0.3), 6, rng=MISS) == []


def test_critical_alarm_kept_without_primary():
    rs = [_mk("a"), _mk("crit", severity=Severity.CRITICAL)]
    app = _app(p={"a": 0.3, "crit": 0.75}, q={"a": 0.35, "crit": 0.3}, none=0.2)
    sel = _apply_jev_dosage(rs, app, max_results=6, rng=MISS)
    assert [r.enzyme.id for r in sel] == ["crit"]


def test_unjudged_candidates_not_selected():
    rs = [_mk("a"), _mk("unjudged")]
    sel = _apply_jev_dosage(rs, _app(p={"a": 0.9}, q={"a": 0.9}), 6, rng=MISS)
    assert [r.enzyme.id for r in sel] == ["a"]


def test_max_results_cap():
    rs = [_mk(f"e{i}") for i in range(5)]
    app = _app(p={f"e{i}": 0.99 for i in range(5)}, q={f"e{i}": 0.2 for i in range(5)})
    assert len(_apply_jev_dosage(rs, app, max_results=2, rng=MISS)) == 2


def test_split_choice_still_injects():
    """近重复记忆把 choice 摊薄到各 0.2:noul 极高且未判 none 时仍要推,不能"都适用反而都不推"。"""
    rs = [_mk(f"dup{i}") for i in range(4)]
    app = _app(p={f"dup{i}": 0.95 for i in range(4)}, q={f"dup{i}": 0.2 for i in range(4)}, none=0.2)
    assert len(_apply_jev_dosage(rs, app, max_results=6, rng=MISS)) == 4
    app_none = _app(p={f"dup{i}": 0.95 for i in range(4)}, q={f"dup{i}": 0.1 for i in range(4)}, none=0.6)
    assert _apply_jev_dosage(rs, app_none, max_results=6, rng=MISS) == []


def test_cold_start_promotion_once():
    rs = [_mk("new1", conf=0.1), _mk("new2", conf=0.1), _mk("old", conf=0.9)]
    app = _app(p={"new1": 0.97, "new2": 0.96, "old": 0.95},
               q={"new1": 0.5, "new2": 0.3, "old": 0.2})
    sel = _apply_jev_dosage(rs, app, max_results=6, rng=MISS)
    flags = {r.enzyme.id: r.exploratory for r in sel}
    assert flags == {"new1": True, "new2": False, "old": False}


def test_no_promotion_for_refuted():
    """被反馈否定跌下来的酶不解锁——学习成果不能被 Jev 的语义判定推翻。"""
    refuted = _mk("refuted", conf=0.1)
    refuted.enzyme.trigger_count, refuted.enzyme.false_positive_count = 3, 3
    veteran = _mk("veteran", conf=0.1)
    veteran.enzyme.trigger_count, veteran.enzyme.success_count = 6, 3
    for r in (refuted, veteran):
        sel = _apply_jev_dosage([r], _app(p={r.enzyme.id: 0.97}, q={r.enzyme.id: 0.9}), 6, rng=MISS)
        assert [x.enzyme.id for x in sel] == [r.enzyme.id] and not sel[0].exploratory


def test_explorer_only_among_plausible():
    main = _mk("main")
    irrelevant = _mk("dream_irrelevant", conf=0.2, origin=Origin.EVOLVED)
    plausible = _mk("dream_plausible", conf=0.2, origin=Origin.EVOLVED)
    app = _app(p={"main": 0.95, "dream_irrelevant": 0.05, "dream_plausible": 0.6},
               q={"main": 0.9})
    sel = _apply_jev_dosage([main, irrelevant, plausible], app, 6, rng=FIRE)
    assert [r.enzyme.id for r in sel] == ["main", "dream_plausible"]
    assert sel[-1].exploratory


# ── run_pipeline 集成(替身 recall / Jev) ─────────────────────────

class FakePG:
    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzymes(n):
    return [Enzyme(id=f"e{i}", patterns=[Pattern(value="浮点")], then=f"t{i}", confidence=0.8)
            for i in range(n)]


async def _run(monkeypatch, judge, n=5, embedding_config_override=None, **kw):
    es = _enzymes(n)

    async def fake_recall(*a, **k):
        return es

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)
    monkeypatch.setattr(pl, "judge_applicability", judge)
    trace: dict = {}
    res = await pl.run_pipeline(context="浮点数比较", search_query="浮点", pg=FakePG(), qdrant=None,
                                embedding_config=embedding_config_override, max_results=6,
                                trace=trace, **kw)
    return res, trace


@pytest.mark.asyncio
async def test_pipeline_uses_jev(monkeypatch):
    async def judge(context, enzymes, cfg, timeout=0, analogies=None):
        return _app(p={e.id: (0.95 if e.id == "e3" else 0.05) for e in enzymes},
                    q={e.id: (0.9 if e.id == "e3" else 0.01) for e in enzymes}, none=0.05)

    res, trace = await _run(monkeypatch, judge, typesafe_config=TS)
    assert [r.enzyme.id for r in res] == ["e3"]
    assert trace["jev_status"] == "ok" and trace["dosed_ids"] == ["e3"]


@pytest.mark.asyncio
async def test_pipeline_falls_back_on_failure(monkeypatch):
    async def judge(*a, **k):
        return None

    res, trace = await _run(monkeypatch, judge, typesafe_config=TS)
    assert res == [] and trace["jev_status"] == "failed"   # 判官失灵:judge.fallback_budget 默认 0,无危急项则不注入


@pytest.mark.asyncio
async def test_pipeline_falls_back_on_timeout(monkeypatch):
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.05")

    async def judge(*a, **k):
        await asyncio.sleep(1)

    res, trace = await _run(monkeypatch, judge, typesafe_config=TS)
    assert res == [] and trace["jev_status"] == "timeout"


@pytest.mark.asyncio
async def test_pipeline_off_without_key_or_mode(monkeypatch):
    called = []

    async def judge(*a, **k):
        called.append(1)

    await _run(monkeypatch, judge, typesafe_config=TypeSafeConfig())      # 无 key
    monkeypatch.setenv("CE_JEV_MODE", "off")
    await _run(monkeypatch, judge, typesafe_config=TS)                      # 显式关闭
    assert called == []


@pytest.mark.asyncio
async def test_pipeline_parallel_stage_pool(monkeypatch):
    monkeypatch.setenv("CE_JEV_STAGE", "parallel")
    monkeypatch.setenv("CE_JEV_POOL", "3")
    seen = []

    async def judge(context, enzymes, cfg, timeout=0, analogies=None):
        seen.append([e.id for e in enzymes])
        return _app(p={e.id: 0.95 for e in enzymes}, q={"e0": 0.9})

    res, _ = await _run(monkeypatch, judge, n=5, typesafe_config=TS)
    assert seen == [["e0", "e1", "e2"]]              # 召回序前 N 条,只判一次
    assert {r.enzyme.id for r in res} <= {"e0", "e1", "e2"}


# ── 客户端解析 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_client_parses_and_tolerates_malformed(monkeypatch):
    import app.typesafe_client as tc
    es = _enzymes(2)
    good = {"model": "jev-1.13.0", "answers": {
        "c0": {"type": "noul", "noul": 0.9}, "c1": {"type": "noul", "noul": 0.1},
        "best": {"type": "choice", "choice": "c0",
                 "probabilities": {"c0": 0.8, "c1": 0.05, "none": 0.15}, "confidence": 0.7}}}
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["q"] = questions
        return good

    monkeypatch.setattr(tc, "system_one", fake)
    app = await judge_applicability("ctx", es, TS)
    assert app.p == {"e0": 0.9, "e1": 0.1} and app.q == {"e0": 0.8, "e1": 0.05}
    assert app.none == 0.15 and app.model == "jev-1.13.0"
    assert set(captured["q"]) == {"c0", "c1", "best"}
    assert set(captured["q"]["best"]["criteria"]) == {"c0", "c1", "none"}

    async def broken(*a, **k):
        return {"answers": {"c0": {"noul": 0.9}}}

    monkeypatch.setattr(tc, "system_one", broken)
    assert await judge_applicability("ctx", es, TS) is None


@pytest.mark.asyncio
async def test_system_one_unconfigured_returns_none():
    from app.typesafe_client import system_one
    assert await system_one("s", {"a": {}}, TypeSafeConfig()) is None


@pytest.mark.asyncio
async def test_rescue_rerank_dropped(monkeypatch):
    """CE_JEV_RESCUE=on:被 rerank/floor 淘汰但 Jev 判适用的候选可救回;关闭时不救。"""
    from app.config import EmbeddingConfig
    from app.matching.rerank import RerankResult

    async def fake_rerank(query, documents, config, top_n=None):
        return [RerankResult(index=0, score=0.9)]           # 只有 e0 过 floor,e1..e4 被淘汰

    async def judge(context, enzymes, cfg, timeout=0, analogies=None):
        return _app(p={e.id: (0.97 if e.id == "e4" else 0.05) for e in enzymes},
                    q={e.id: (0.9 if e.id == "e4" else 0.01) for e in enzymes}, none=0.05)

    monkeypatch.setattr(pl, "rerank", fake_rerank)
    emb = EmbeddingConfig(api_key="k", api_url="u", rerank_api_url="r")
    for flag, expect in (("off", []), ("on", ["e4"])):
        monkeypatch.setenv("CE_JEV_RESCUE", flag)
        res, trace = await _run(monkeypatch, judge, typesafe_config=TS, embedding_config_override=emb)
        assert [r.enzyme.id for r in res] == expect, flag


# ── 适用性感知学分 ───────────────────────────────────────────────

def test_failure_weight_curve():
    from app.core.engine import failure_weight
    assert failure_weight(None) == 1.0
    assert abs(failure_weight(0.3) - 0.95) < 1e-9 and abs(failure_weight(0.95) - 1.925) < 1e-9


@pytest.mark.asyncio
async def test_hebbian_failure_scaled_by_applicability(monkeypatch):
    from app.config import Config
    from app.core.engine import Engine

    class PG:
        def __init__(self):
            self.e = {}
        async def get_enzyme(self, eid):
            return self.e.get(eid)
        async def log_event(self, ev):
            pass

    pg = PG()
    engine = Engine(Config(), pg, qdrant=None)

    async def no_update(enzyme, expected_version=None):
        pg.e[enzyme.id] = enzyme
    monkeypatch.setattr(engine, "update_enzyme", no_update)

    deltas = {}
    for eid, p in (("hi", 0.95), ("lo", 0.3), ("unknown", None)):
        pg.e[eid] = Enzyme(id=eid, patterns=[Pattern(value="x")], then="t", confidence=0.5)
        if p is not None:
            r = MatchResult(enzyme=pg.e[eid], score=0.5, tier="relevant", applicability=p)
            engine._remember_applicability([r])
        deltas[eid] = (await engine.hebbian_update(eid, success=False, expected=False))["delta"]
    assert deltas["unknown"] == -0.05
    assert deltas["hi"] < deltas["unknown"] < deltas["lo"] < 0   # 高适用失败重罚、弱匹配轻罚

    monkeypatch.setenv("CE_JEV_CREDIT", "off")
    pg.e["hi"].confidence = 0.5
    assert (await engine.hebbian_update("hi", success=False, expected=False))["delta"] == -0.05


def test_rescued_needs_strong_evidence():
    """被精排淘汰的候选:两个判官意见相反,p 未达 CE_JEV_RESCUE_P 不得入选(含 critical 路径)。"""
    weak = _mk("weak")
    crit = _mk("crit", severity=Severity.CRITICAL)
    strong = _mk("strong")
    app = _app(p={"weak": 0.88, "crit": 0.8, "strong": 0.95}, q={"weak": 0.53, "crit": 0.0, "strong": 0.0})
    assert _apply_jev_dosage([weak, crit], app, 6, rng=MISS, rescued_ids={"weak", "crit"}) == []
    assert [r.enzyme.id for r in _apply_jev_dosage([weak, crit], app, 6, rng=MISS)] == ["weak", "crit"]
    app2 = _app(p={"strong": 0.95}, q={"strong": 0.9})
    assert [r.enzyme.id for r in _apply_jev_dosage([strong], app2, 6, rng=MISS, rescued_ids={"strong"})] == ["strong"]


@pytest.mark.asyncio
async def test_parallel_deadline_counts_from_request_start(monkeypatch):
    """parallel 模式:Jev 超时从请求发出时起算——rerank 已耗掉的时间计入预算。"""
    from app.config import EmbeddingConfig
    from app.matching.rerank import RerankResult
    import time as _t
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.3")
    monkeypatch.setenv("CE_JEV_STAGE", "parallel")

    async def slow_rerank(query, documents, config, top_n=None):
        await asyncio.sleep(0.25)
        return [RerankResult(index=i, score=0.9) for i in range(len(documents))]

    async def judge(*a, **k):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(pl, "rerank", slow_rerank)
    emb = EmbeddingConfig(api_key="k", api_url="u", rerank_api_url="r")
    t0 = _t.perf_counter()
    res, trace = await _run(monkeypatch, judge, typesafe_config=TS, embedding_config_override=emb)
    elapsed = _t.perf_counter() - t0
    assert trace["jev_status"] == "timeout" and res == []
    assert elapsed < 0.45   # ≈ max(rerank 0.25, timeout 0.3),而不是 0.25 + 0.3


# ── LLM 第二判官 ─────────────────────────────────────────────────

def test_llm_judge_parse():
    from app.matching.llm_judge import parse_probs
    es = _enzymes(3)
    assert parse_probs('```json\n{"c0": 0.9, "c1": "0.2"}\n```', es) == {"e0": 0.9, "e1": 0.2, "e2": 0.0}
    assert parse_probs('{"c0": 1.7, "c1": -1, "c2": "x"}', es) == {"e0": 1.0, "e1": 0.0, "e2": 0.0}
    assert parse_probs("抱歉,无法判断", es) is None and parse_probs("{}", es) is None


LLM = __import__("app.config", fromlist=["LLMConfig"]).LLMConfig(api_key="k", api_url="https://llm.invalid/v1")


@pytest.mark.asyncio
async def test_llm_judge_fused_with_jev(monkeypatch):
    monkeypatch.setenv("CE_JUDGE_LLM", "on")

    async def jev(context, enzymes, cfg, timeout=0, analogies=None):
        # Jev 对 e1 拿不准(0.6),单选偏 e1;LLM 很确定 e1 有用 → 融合后 p=0.8
        return _app(p={e.id: (0.6 if e.id == "e1" else 0.05) for e in enzymes},
                    q={e.id: (0.7 if e.id == "e1" else 0.02) for e in enzymes}, none=0.1)

    async def llm(context, enzymes, cfg, timeout=0):
        return {e.id: (1.0 if e.id == "e1" else 0.0) for e in enzymes}

    monkeypatch.setattr(pl, "judge_llm", llm)
    res, trace = await _run(monkeypatch, jev, typesafe_config=TS, llm_config=LLM)
    assert [r.enzyme.id for r in res] == ["e1"] and res[0].applicability == 0.8
    assert trace["llm_judge_status"] == "ok"


@pytest.mark.asyncio
async def test_llm_judge_failure_keeps_jev(monkeypatch):
    monkeypatch.setenv("CE_JUDGE_LLM", "on")

    async def jev(context, enzymes, cfg, timeout=0, analogies=None):
        return _app(p={e.id: (0.95 if e.id == "e2" else 0.05) for e in enzymes},
                    q={e.id: (0.9 if e.id == "e2" else 0.01) for e in enzymes}, none=0.05)

    async def llm(*a, **k):
        return None

    monkeypatch.setattr(pl, "judge_llm", llm)
    res, trace = await _run(monkeypatch, jev, typesafe_config=TS, llm_config=LLM)
    assert [r.enzyme.id for r in res] == ["e2"] and res[0].applicability == 0.95
    assert trace["llm_judge_status"] == "failed"


@pytest.mark.asyncio
async def test_llm_judge_off_by_default(monkeypatch):
    called = []

    async def jev(context, enzymes, cfg, timeout=0, analogies=None):
        return _app(p={e.id: 0.95 for e in enzymes}, q={"e0": 0.9})

    async def llm(*a, **k):
        called.append(1)

    monkeypatch.setattr(pl, "judge_llm", llm)
    await _run(monkeypatch, jev, typesafe_config=TS, llm_config=LLM)
    assert called == []



@pytest.mark.asyncio
async def test_hebbian_rw_rule(monkeypatch):
    """rw:幅度 ∝ 预测误差;失败按适用概率加权,成功权重 1。"""
    from app.config import Config
    from app.core.engine import Engine
    monkeypatch.setenv("CE_HEBBIAN_RULE", "rw")

    class PG:
        def __init__(self):
            self.e = {}
        async def get_enzyme(self, eid):
            return self.e.get(eid)
        async def log_event(self, ev):
            pass

    pg = PG()
    engine = Engine(Config(), pg, qdrant=None)

    async def upd(enzyme, expected_version=None):
        pg.e[enzyme.id] = enzyme
    monkeypatch.setattr(engine, "update_enzyme", upd)

    def mk(eid, conf, p=None):
        pg.e[eid] = Enzyme(id=eid, patterns=[Pattern(value="x")], then="t", confidence=conf)
        if p is not None:
            engine._remember_applicability([MatchResult(enzyme=pg.e[eid], score=0.5, tier="r", applicability=p)])

    mk("s", 0.5)
    assert abs((await engine.hebbian_update("s", success=True))["delta"] - 0.15) < 1e-9       # 0.3·(1−0.5)
    mk("f_hi", 0.5, p=1.0)
    mk("f_lo", 0.5, p=0.4)
    d_hi = (await engine.hebbian_update("f_hi", success=False))["delta"]
    d_lo = (await engine.hebbian_update("f_lo", success=False))["delta"]
    assert abs(d_hi + 0.15) < 1e-9 and abs(d_lo + 0.06) < 1e-9                              # 0.3·p·(0−0.5)



@pytest.mark.asyncio
async def test_prompt_v2_switch(monkeypatch):
    import app.typesafe_client as tc
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["q"] = questions
        return None

    monkeypatch.setattr(tc, "system_one", fake)
    await judge_applicability("ctx", _enzymes(1), TS)
    assert "建议" in captured["q"]["c0"]["instructions"]["rule"]            # 默认 v1
    monkeypatch.setenv("CE_JEV_PROMPT", "v2")
    await judge_applicability("ctx", _enzymes(1), TS)
    rule = captured["q"]["c0"]["instructions"]["rule"]
    assert "内容" in rule and "建议" not in rule
    assert "反驳" in captured["q"]["c0"]["criteria"]["true"]
    # v3:逐条 noul 用 v1 问法,单选题用 v2 问法
    monkeypatch.setenv("CE_JEV_PROMPT", "v3")
    await judge_applicability("ctx", _enzymes(1), TS)
    q = captured["q"]
    assert "建议" in q["c0"]["instructions"]["rule"] and "反驳" not in q["c0"]["criteria"]["true"]
    assert "内容" in q["best"]["criteria"]["c0"] and "相关" in q["best"]["instructions"]



def test_confident_choice_overrides_low_noul():
    """知识类记忆:choice 够确定(q≥0.6)时即使 noul 低也主推;被淘汰候选要 q≥0.7 才可救回。"""
    rs = [_mk("bug"), _mk("other")]
    app = _app(p={"bug": 0.21, "other": 0.1}, q={"bug": 0.65, "other": 0.02}, none=0.33)
    assert [r.enzyme.id for r in _apply_jev_dosage(rs, app, 6, rng=MISS)] == ["bug"]
    app_weak = _app(p={"bug": 0.21, "other": 0.1}, q={"bug": 0.55, "other": 0.1}, none=0.35)
    assert _apply_jev_dosage(rs, app_weak, 6, rng=MISS) == []
    resc = _mk("resc")
    for q, picked in ((0.65, []), (0.75, ["resc"])):   # 救回门比主选门更严
        app_r = _app(p={"resc": 0.59}, q={"resc": q}, none=1 - q)
        got = _apply_jev_dosage([resc], app_r, 6, rng=MISS, rescued_ids={"resc"})
        assert [r.enzyme.id for r in got] == picked


def test_offline_select_matches_pipeline():
    """离线评估老师 / 导出用的 selection.select 与线上 _apply_jev_dosage 必须是同一条规则
    (不含危急级 / 类比 / 探索通道):随机的判官回答下两边选出的条目与顺序完全一致。"""
    import random as _random
    from app.matching.selection import select
    rnd = _random.Random(7)
    for _ in range(3000):
        n = rnd.randint(1, 8)
        ids = [f"c{i}" for i in range(n)]
        p = {k: round(rnd.random() ** rnd.choice((0.3, 1, 2)), 2) for k in ids}
        sharp = rnd.choice((1, 3, 8))                      # 让单选概率时而平摊、时而一家独大
        raw = [rnd.random() ** sharp for _ in range(n + 1)]
        q = {k: raw[i] / sum(raw) for i, k in enumerate(ids)}
        none = raw[-1] / sum(raw)
        rescued = {k for k in ids if rnd.random() < 0.25}
        got = _apply_jev_dosage([_mk(k) for k in ids], _app(p=p, q=q, none=none), 6,
                                rng=MISS, rescued_ids=rescued)
        assert [r.enzyme.id for r in got] == select(p, q, none, rescued), (p, q, none, rescued)


@pytest.mark.asyncio
async def test_analogy_questions_parsed_and_isolated(monkeypatch):
    """类比识别:带过往情境的候选额外问 noul + choice;类比题畸形只丢类比结果。"""
    import app.typesafe_client as tc
    es = _enzymes(2)
    es[0]._hint = "上次 139 升级后侧边栏状态丢失"
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["q"] = questions
        ans = {f"c{i}": {"noul": 0.2} for i in range(2)}
        ans["best"] = {"probabilities": {"c0": 0.3, "c1": 0.2, "none": 0.5}}
        ans["a0"] = {"noul": 0.85}
        ans["same"] = {"probabilities": {"a0": 0.9, "none": 0.1}}
        return {"answers": ans}
    monkeypatch.setattr(tc, "system_one", fake)
    app = await judge_applicability("ctx", es, TS, analogies=[es[0], es[1]])   # es[1] 没有过往情境,跳过
    assert set(captured["q"]) == {"c0", "c1", "best", "a0", "same"}
    assert captured["q"]["a0"]["instructions"]["past"] == es[0]._hint
    assert app.analog_q == {es[0].id: 0.9} and app.analog_p == {es[0].id: 0.85} and app.analog_none == 0.1

    async def broken(state, questions, config, timeout=0):
        ans = {f"c{i}": {"noul": 0.2} for i in range(2)}
        ans["best"] = {"probabilities": {"c0": 0.3, "none": 0.7}}
        return {"answers": ans}                                           # 类比题缺失
    monkeypatch.setattr(tc, "system_one", broken)
    app = await judge_applicability("ctx", es, TS, analogies=[es[0]])
    assert app is not None and app.p and app.analog_q == {}


def test_analogy_channel(monkeypatch):
    rs = [_mk("seen"), _mk("other")]
    app = _app(p={"seen": 0.2, "other": 0.3}, q={"seen": 0.1, "other": 0.2}, none=0.7)
    app.analog_q, app.analog_p, app.analog_none = {"seen": 0.82}, {"seen": 0.7}, 0.18
    assert [r.enzyme.id for r in _apply_jev_dosage(rs, app, 6, rng=MISS)] == ["seen"]   # 默认 on
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "shadow")
    assert _apply_jev_dosage(rs, app, 6, rng=MISS) == []                     # shadow 只问不用
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "off")
    assert _apply_jev_dosage(rs, app, 6, rng=MISS) == []
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "on")
    app.analog_q, app.analog_none = {"seen": 0.6}, 0.4                       # 单选题不够确定
    assert _apply_jev_dosage(rs, app, 6, rng=MISS) == []
    app.analog_q, app.analog_p, app.analog_none = {"seen": 0.9}, {"seen": 0.4}, 0.1   # 逐条判断不认
    assert _apply_jev_dosage(rs, app, 6, rng=MISS) == []


def test_analogy_candidates_top_k_by_hint(monkeypatch):
    es = [_mk(f"e{i}").enzyme for i in range(5)]
    for e, s in zip(es, (0.5, 0.9, 0.0, 0.7, 0.6)):
        if s:
            e._hint, e._hint_score = f"past {e.id}", s
    assert [e.id for e in pl._analogy_candidates(es)] == ["e1", "e3", "e4"]  # 默认 on:取提示分前 3
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "shadow")
    assert [e.id for e in pl._analogy_candidates(es)] == ["e1", "e3", "e4"]
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "off")
    assert pl._analogy_candidates(es) == []                                  # off 不问
