"""经验样本(System 1 式识别)单测 — 纯内存替身。

覆盖:召回合并(本体/样本取最高分、样本带来的新召回、提示取最像的那条)、提示进入
Jev 规则与精排文档、Engine 学习(成功即记、去重、上限淘汰最旧、回声不学、开关)、
capture 归并印证时显式学习、SyncManager 增量同步(只嵌入新增、删除移出的)。
"""
import dataclasses

import pytest

from app.config import Config, TypeSafeConfig
from app.core import Enzyme, Pattern
from app.core.engine import Engine
from app.matching import pipeline as pl
from app.matching.pipeline import MatchResult, _merge_exemplar_recall
from app.storage.qdrant_store import exemplar_point_id
from app.typesafe_client import _rule


def _e(eid, **kw):
    return Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}", **kw)


def test_merge_takes_max_and_adds_exemplar_only_recalls():
    hits = [{"id": "a", "_score": 0.40}, {"id": "b", "_score": 0.55}]
    ex = [{"id": "a", "_score": 0.90, "text": "上次那个 a 的情境"}, {"id": "a", "_score": 0.70, "text": "较远"},
          {"id": "c", "_score": 0.60, "text": "c 的情境"}]
    ids, hints = _merge_exemplar_recall(hits, ex)
    assert ids == ["a", "c", "b"]                       # a 靠样本升到最前;c 只被样本召回
    assert hints == {"a": "上次那个 a 的情境", "c": "c 的情境"}


def test_hint_goes_into_jev_rule(monkeypatch):
    e = _e("a")
    assert "过往适用情境" not in _rule(e)
    e._hint = "上次用户说 X 然后崩溃"
    assert _rule(e)["过往适用情境"] == "上次用户说 X 然后崩溃"
    monkeypatch.setenv("CE_EXEMPLAR_HINT", "off")
    assert "过往适用情境" not in _rule(e)


class FakePG:
    def __init__(self, *es):
        self.e = {x.id: x for x in es}
        self.events = []

    async def get_enzyme(self, eid):
        return self.e.get(eid)

    async def log_event(self, ev):
        self.events.append(ev)


def _engine(monkeypatch, *es):
    pg = FakePG(*es)
    engine = Engine(Config(), pg, qdrant=None)

    async def upd(enzyme, expected_version=None):
        pg.e[enzyme.id] = enzyme
    monkeypatch.setattr(engine, "update_enzyme", upd)
    return engine, pg


@pytest.mark.asyncio
async def test_success_feedback_learns_prime_context(monkeypatch):
    engine, pg = _engine(monkeypatch, _e("a", confidence=0.5))
    ctx = "Firefox 139 重启后侧边栏按钮总是回到书签,而不是上次打开的历史"
    engine._remember_contexts(ctx, [MatchResult(enzyme=pg.e["a"], score=0.5, tier="r")])
    res = await engine.hebbian_update("a", success=True)
    assert pg.e["a"].exemplars == [ctx]
    # 同一情境再次成功不重复记
    await engine.hebbian_update("a", success=True)
    assert pg.e["a"].exemplars == [ctx]
    assert res  # 正常返回


@pytest.mark.asyncio
async def test_failure_and_echo_do_not_learn(monkeypatch):
    engine, pg = _engine(monkeypatch, _e("a"), _e("b"))
    ctx = "一个足够长的真实情境描述,用于测试不该被学到的情况"
    engine._remember_contexts(ctx, [MatchResult(enzyme=pg.e["a"], score=0.5, tier="r"),
                                    MatchResult(enzyme=pg.e["b"], score=0.5, tier="r")])
    await engine.hebbian_update("a", success=False)
    await engine.hebbian_update("b", success=True, credit=0.5, update_stats=False)   # 回声
    assert pg.e["a"].exemplars == [] and pg.e["b"].exemplars == []


@pytest.mark.asyncio
async def test_learn_exemplar_bounded_and_switchable(monkeypatch):
    monkeypatch.setenv("CE_EXEMPLAR_MAX", "3")
    engine, pg = _engine(monkeypatch, _e("a"))
    for i in range(5):
        assert await engine.learn_exemplar("a", f"第 {i} 次遇到的真实情境,描述足够长以便记录", source="t")
    assert [x[2] for x in pg.e["a"].exemplars] == ["2", "3", "4"]     # 淘汰最旧的
    assert not await engine.learn_exemplar("a", "太短")
    assert pg.events and pg.events[-1].detail["source"] == "t"
    monkeypatch.setenv("CE_EXEMPLAR_LEARN", "off")
    assert not await engine.learn_exemplar("a", "开关关闭时不学习的一条足够长的情境描述")


@pytest.mark.asyncio
async def test_capture_reinforce_learns_trigger(monkeypatch):
    from app.learning import capture
    engine, pg = _engine(monkeypatch, _e("a"))
    summary = {"reinforced": 0, "errors": 0}
    await capture._apply(engine, {"then": "t", "trigger": "用户升级到 139 后侧边栏状态丢失的会话情境"},
                         {"action": "REINFORCE", "target_id": "a"}, summary)
    assert pg.e["a"].exemplars == ["用户升级到 139 后侧边栏状态丢失的会话情境"]


@pytest.mark.asyncio
async def test_sync_exemplars_incremental(monkeypatch):
    from app.storage import sync as sync_mod

    embedded = []

    async def fake_embed(text, config=None):
        embedded.append(text)
        return [1.0]

    class Q:
        embedding_config = None

        def __init__(self, existing):
            self.existing = set(existing)
            self.upserted, self.deleted = [], []

        async def exemplar_point_ids(self, eid):
            return set(self.existing)

        async def upsert_exemplars(self, enzyme, items, kind="pos"):
            self.upserted += [t for t, _ in items]

        async def delete_exemplar_points(self, ids):
            self.deleted += ids

    monkeypatch.setattr(sync_mod, "embed", fake_embed)
    e = _e("a", exemplars=["保留的旧样本情境文本", "新增的样本情境文本"])
    stale = exemplar_point_id("a", "已被淘汰的样本")
    q = Q({exemplar_point_id("a", "保留的旧样本情境文本"), stale})
    await sync_mod.SyncManager(pg=None, qdrant=q).sync_exemplars(e)
    assert embedded == ["新增的样本情境文本"] and q.upserted == ["新增的样本情境文本"]
    assert q.deleted == [stale]


@pytest.mark.asyncio
async def test_recall_uses_exemplars(monkeypatch):
    a, b = _e("a"), _e("b")

    class Q:
        async def search(self, vector, domain=None, limit=50):
            return [{"id": "a", "_score": 0.3}]

        async def search_exemplars(self, vector, domain=None, limit=30):
            return [{"id": "b", "_score": 0.9, "text": "b 的过往情境"}]

    class PG:
        async def get_enzymes(self, ids):
            return [{"a": a, "b": b}[i] for i in ids]

    async def fake_embed(text, config=None):
        return [0.1]

    monkeypatch.setattr("app.llm_client.embed", fake_embed)
    got = await pl._recall("q", "ctx", PG(), Q(), None, None)
    assert [e.id for e in got] == ["b", "a"] and got[0]._hint == "b 的过往情境"
    monkeypatch.setenv("CE_EXEMPLAR_RECALL", "off")
    got = await pl._recall("q", "ctx", PG(), Q(), None, None)
    assert [e.id for e in got] == ["a"]


@pytest.mark.asyncio
async def test_failure_learns_anti_exemplar_and_latest_outcome_wins(monkeypatch):
    monkeypatch.setenv("CE_ANTI_EXEMPLAR", "on")
    engine, pg = _engine(monkeypatch, _e("a"))
    ctx = "这是一个足够长的真实情境,推送了 a 但被证明不适用"
    engine._remember_contexts(ctx, [MatchResult(enzyme=pg.e["a"], score=0.5, tier="r")])
    await engine.hebbian_update("a", success=False)
    assert pg.e["a"].anti_exemplars == [ctx] and pg.e["a"].exemplars == []
    await engine.hebbian_update("a", success=True)            # 同一情境后来被证实有用 → 以最新为准
    assert pg.e["a"].exemplars == [ctx] and pg.e["a"].anti_exemplars == []


def test_anti_hint_goes_into_jev_rule():
    e = _e("a")
    e._anti_hint = "上次类似情况这条不适用"
    assert _rule(e)["过往不适用情境"] == "上次类似情况这条不适用"


def test_evict_most_redundant_keeps_diverse():
    from app.core.engine import _evict_redundant
    items = ["Firefox 侧边栏在重启后回到书签页面", "Firefox 侧边栏重启后总是回到书签页面",
             "判决书引用了关于 ERISA 计划精算稳健性的段落"]
    kept = _evict_redundant(items, 2)
    assert "判决书引用了关于 ERISA 计划精算稳健性的段落" in kept and len(kept) == 2   # 淘汰近重复,保留多样


@pytest.mark.asyncio
async def test_recall_anti_hits_only_hint(monkeypatch):
    monkeypatch.setenv("CE_ANTI_EXEMPLAR", "on")
    a = _e("a")

    class Q:
        async def search(self, vector, domain=None, limit=50):
            return [{"id": "a", "_score": 0.3}]

        async def search_exemplars(self, vector, domain=None, limit=30):
            return [{"id": "a", "_score": 0.8, "text": "a 的反例情境", "kind": "anti"},
                    {"id": "z", "_score": 0.9, "text": "z 的反例", "kind": "anti"}]

    class PG:
        async def get_enzymes(self, ids):
            return [a for i in ids if i == "a"]

    async def fake_embed(text, config=None):
        return [0.1]

    monkeypatch.setattr("app.llm_client.embed", fake_embed)
    got = await pl._recall("q", "ctx", PG(), Q(), None, None)
    assert [e.id for e in got] == ["a"]                      # 反例不带来新召回(z 未被召回)
    assert got[0]._anti_hint == "a 的反例情境" and got[0]._hint == ""


@pytest.mark.asyncio
async def test_exemplar_is_scrubbed(monkeypatch):
    engine, pg = _engine(monkeypatch, _e("a"))
    secret = "sk-ant-" + "A" * 30
    assert await engine.learn_exemplar("a", f"部署时用 key {secret} 调接口一直 401,不知道哪里错了")
    assert secret not in pg.e["a"].exemplars[0] and "[REDACTED]" in pg.e["a"].exemplars[0]


@pytest.mark.asyncio
async def test_anti_exemplar_off_by_default(monkeypatch):
    engine, pg = _engine(monkeypatch, _e("a"))
    ctx = "默认关闭时,失败反馈不应记下这条足够长的反例情境"
    engine._remember_contexts(ctx, [MatchResult(enzyme=pg.e["a"], score=0.5, tier="r")])
    await engine.hebbian_update("a", success=False)
    assert pg.e["a"].anti_exemplars == []


# ── 查询扩写 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_expansion_merge_rrf_and_max(monkeypatch):
    import asyncio as aio

    async def fake_embed(text, config=None):
        return [0.2]

    class Q:
        async def search(self, vector, domain=None, limit=50):
            return [{"id": "x", "_score": 0.95}, {"id": "gold", "_score": 0.90}]

    monkeypatch.setattr("app.llm_client.embed", fake_embed)
    orig = [{"id": "a", "_score": 0.5}, {"id": "b", "_score": 0.4}]

    async def text():
        return "a knowledge-base style description"
    trace: dict = {}
    monkeypatch.setenv("CE_QUERY_EXPAND_MERGE", "rrf")
    rrf = await pl._merge_expansion(orig, aio.ensure_future(text()), Q(), None, None, trace)
    assert [h["id"] for h in rrf][:2] in (["a", "x"], ["x", "a"]) and {"gold", "b"} <= {h["id"] for h in rrf}
    assert trace["expansion"].startswith("a knowledge")
    monkeypatch.setenv("CE_QUERY_EXPAND_MERGE", "max")
    mx = await pl._merge_expansion(orig, aio.ensure_future(text()), Q(), None, None, None)
    assert [h["id"] for h in mx] == ["x", "gold", "a", "b"]

    async def failed():
        return None
    assert await pl._merge_expansion(orig, aio.ensure_future(failed()), Q(), None, None, None) == orig


def test_expansion_off_by_default():
    from app.config import LLMConfig
    from app.matching.expand import expand_on
    cfg = LLMConfig(api_key="k", api_url="u")
    assert not expand_on(cfg)
    import os
    os.environ["CE_QUERY_EXPAND"] = "on"
    try:
        assert expand_on(cfg) and not expand_on(LLMConfig())
    finally:
        del os.environ["CE_QUERY_EXPAND"]


def test_exemplar_votes_top10():
    hits = [{"id": "a", "_score": 0.9 - i * 0.01} for i in range(6)] + \
           [{"id": "b", "_score": 0.5 - i * 0.01} for i in range(6)]
    assert pl._exemplar_votes(hits) == {"a": 6, "b": 4}


@pytest.mark.asyncio
async def test_capture_reinforce_prefers_session_context(monkeypatch):
    """会话里有 prime 情境原文且 Jev 能认出是哪一轮 → 学情境原文;认不出 → 退回触发描述。"""
    import app.typesafe_client as tc
    from app.learning import capture
    engine, pg = _engine(monkeypatch, _e("a"))
    engine.config = dataclasses.replace(engine.config, typesafe=TypeSafeConfig(api_key="k"))
    ctxs = ["升级 139 之后侧边栏的展开状态每次重启都丢了,怎么回事", "帮我写个周报"]

    async def session_contexts(sid, limit=30):
        return ctxs if sid == "s1" else []
    pg.session_contexts = session_contexts
    answer = {"s0": 0.82, "s1": 0.03, "none": 0.15}

    async def fake(state, questions, config, timeout=0):
        assert set(questions["where"]["criteria"]) == {"s0", "s1", "none"}
        return {"answers": {"where": {"probabilities": answer}}}
    monkeypatch.setattr(tc, "system_one", fake)
    trigger = "用户升级到 139 后侧边栏状态丢失的会话情境"
    await capture._apply(engine, {"then": "t", "trigger": trigger},
                         {"action": "REINFORCE", "target_id": "a"}, {"reinforced": 0, "errors": 0},
                         session_id="s1")
    assert pg.e["a"].exemplars == [ctxs[0]]
    assert pg.events[-1].detail["source"] == "capture_reinforce_context"

    answer.update({"s0": 0.3, "none": 0.67})                       # 都不适用 → 退回触发描述
    engine2, pg2 = _engine(monkeypatch, _e("b"))
    engine2.config = engine.config
    pg2.session_contexts = session_contexts
    await capture._apply(engine2, {"then": "t", "trigger": trigger},
                         {"action": "REINFORCE", "target_id": "b"}, {"reinforced": 0, "errors": 0},
                         session_id="s1")
    assert pg2.e["b"].exemplars == [trigger]


@pytest.mark.asyncio
async def test_record_session_context_scrubbed(monkeypatch):
    engine, pg = _engine(monkeypatch)
    got = []

    async def record(sid, ctx):
        got.append((sid, ctx))
    pg.record_prime_context = record
    await engine.record_session_context("s1", "部署时报错   key=sk-abcdefghijklmnopqrstuvwxyz0123456789 怎么办")
    await engine.record_session_context("", "没有会话 id 不记")
    assert len(got) == 1 and got[0][0] == "s1"
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in got[0][1] and "   " not in got[0][1]


@pytest.mark.asyncio
async def test_passive_helped_learns_session_context_when_cache_cold(monkeypatch):
    """被动评估判 helped,但注入时的情境已不在进程内缓存 → 从会话 prime 情境原文里挑一条学。"""
    import app.typesafe_client as tc
    from app.learning import capture
    engine, pg = _engine(monkeypatch, _e("a"))
    engine.config = dataclasses.replace(engine.config, typesafe=TypeSafeConfig(api_key="k"))
    ctx = "升级 139 之后侧边栏的展开状态每次重启都丢了,怎么回事"
    recorded = []

    async def session_contexts(sid, limit=30):
        return [ctx]

    async def filter_unevaluated(sid, ids):
        return ids

    async def record_eval(sid, eid, verdict):
        recorded.append((sid, eid, verdict))
    pg.session_contexts, pg.filter_unevaluated, pg.record_eval = session_contexts, filter_unevaluated, record_eval

    async def fake_chat_json(system, user, config=None):
        return [{"id": "a", "verdict": "helped", "evidence": "照做后解决了"}]

    async def fake_jev(state, questions, config, timeout=0):
        return {"answers": {"where": {"probabilities": {"s0": 0.9, "none": 0.1}}}}
    monkeypatch.setattr(capture, "chat_json", fake_chat_json)
    monkeypatch.setattr(tc, "system_one", fake_jev)
    summary = {"passive_helped": 0, "passive_contradicted": 0, "passive_neutral": 0}
    await capture.evaluate_injections(engine, "transcript", ["a"], "s1", summary)
    assert summary["passive_helped"] == 1 and recorded == [("s1", "a", "helped")]
    assert pg.e["a"].exemplars == [ctx]
