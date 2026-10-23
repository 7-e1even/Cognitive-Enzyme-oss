"""run_pipeline 的 trace 参数应记录每个阶段的候选 id。"""
import pytest

from app.config import EmbeddingConfig
from app.core import Enzyme, Pattern
from app.matching import pipeline as pl
from app.matching.rerank import RerankResult
from app.matching.scorer import compute_score


class FakePG:
    async def get_related(self, enzyme_id, hops=1, limit=5):
        return []

    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzyme(eid: str, conditions: list[str] | None = None) -> Enzyme:
    return Enzyme(
        id=eid,
        patterns=[Pattern(value="浮点")],
        then="浮点比较用容差",
        because="精度误差",
        conditions=conditions or [],
    )


@pytest.mark.asyncio
async def test_trace_records_stages(monkeypatch):
    """CE_CONDITIONS_MODE=hard(显式,默认已是 soft):未命中的 condition 淘汰候选。"""
    monkeypatch.setenv("CE_CONDITIONS_MODE", "hard")
    e1 = _enzyme("e1")
    e2 = _enzyme("e2", conditions=["绝不出现的条件词xyzzy"])  # 会被 check_conditions 杀掉

    async def fake_recall(*args, **kwargs):
        return [e1, e2]

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    trace: dict = {}
    results = await pl.run_pipeline(
        context="浮点数比较的坑",
        search_query="浮点 比较",
        pg=FakePG(),
        qdrant=None,           # _recall 已被替换，不会用到
        embedding_config=None,  # 跳过 rerank
        max_results=6,
        trace=trace,
    )

    assert trace["recalled_ids"] == ["e1", "e2"]
    assert trace["condition_killed_ids"] == ["e2"]
    assert trace["refined_ids"] == ["e1"]
    assert trace["reranked_ids"] == ["e1"]  # 无 rerank 时等于 refined
    assert [s[0] for s in trace["scored"]] == ["e1"]
    assert trace["dosed_ids"] == [r.enzyme.id for r in results] == ["e1"]


@pytest.mark.asyncio
async def test_soft_conditions_do_not_kill(monkeypatch):
    """默认(soft):条件不满足也不淘汰候选(仅 hard 模式才淘汰)。"""
    monkeypatch.delenv("CE_CONDITIONS_MODE", raising=False)
    e1 = _enzyme("e1")
    e2 = _enzyme("e2", conditions=["绝不出现的条件词xyzzy"])  # soft 模式下不再被淘汰

    async def fake_recall(*args, **kwargs):
        return [e1, e2]

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    trace: dict = {}
    await pl.run_pipeline(
        context="浮点数比较的坑",
        search_query="浮点 比较",
        pg=FakePG(),
        qdrant=None,
        embedding_config=None,
        max_results=6,
        trace=trace,
    )

    assert trace["condition_killed_ids"] == []
    assert set(trace["refined_ids"]) == {"e1", "e2"}


@pytest.mark.asyncio
async def test_trace_none_is_noop(monkeypatch):
    async def fake_recall(*args, **kwargs):
        return []
    monkeypatch.setattr(pl, "_recall", fake_recall)
    assert await pl.run_pipeline(
        context="x", search_query="x", pg=FakePG(), qdrant=None,
    ) == []


def test_relevance_floor_env_guarded(monkeypatch):
    """CE_RELEVANCE_FLOOR 非法值应回退默认 0.01 而非炸掉。"""
    monkeypatch.setenv("CE_RELEVANCE_FLOOR", "not_a_number")
    assert pl._relevance_floor() == 0.01


def test_relevance_floor_valid_env(monkeypatch):
    """CE_RELEVANCE_FLOOR 有效数值应被解析。"""
    monkeypatch.setenv("CE_RELEVANCE_FLOOR", "0.05")
    assert pl._relevance_floor() == 0.05


def test_relevance_floor_default(monkeypatch):
    """CE_RELEVANCE_FLOOR 未设置时应用默认值。"""
    monkeypatch.delenv("CE_RELEVANCE_FLOOR", raising=False)
    assert pl._relevance_floor() == 0.01


def test_dosage_budget_env_guarded(monkeypatch):
    """CE_DOSAGE_BUDGET 非法值应回退默认 5,合法值原样解析。"""
    monkeypatch.setenv("CE_DOSAGE_BUDGET", "x")
    assert pl._dosage_budget() == 5
    monkeypatch.setenv("CE_DOSAGE_BUDGET", "8")
    assert pl._dosage_budget() == 8


def test_conditions_boost_stage_default(monkeypatch):
    """CE_CONDITIONS_BOOST_STAGE 未设置或非法值都应回退 "pattern"(现状行为)。"""
    monkeypatch.delenv("CE_CONDITIONS_BOOST_STAGE", raising=False)
    assert pl._condition_boost_stage() == "pattern"
    monkeypatch.setenv("CE_CONDITIONS_BOOST_STAGE", "garbage")
    assert pl._condition_boost_stage() == "pattern"
    monkeypatch.setenv("CE_CONDITIONS_BOOST_STAGE", "rerank")
    assert pl._condition_boost_stage() == "rerank"


@pytest.mark.asyncio
async def test_conditions_boost_rerank_stage_applies_after_replacement(monkeypatch):
    """CE_CONDITIONS_BOOST_STAGE=rerank:rerank 用 rr.score 整体替换分数后,
    命中 conditions 的候选应再补一次加成——默认 "pattern" 模式下这个加成会被
    rerank 的整体替换吃掉(round-2 sweep 证实的死代码路径)。"""
    monkeypatch.setenv("CE_CONDITIONS_BOOST_STAGE", "rerank")
    monkeypatch.delenv("CE_CONDITIONS_MODE", raising=False)  # 默认 soft
    e1 = _enzyme("e1", conditions=["浮点"])  # context 命中该条件词

    async def fake_recall(*args, **kwargs):
        return [e1]

    async def fake_rerank(query, documents, config, top_n=None):
        return [RerankResult(index=0, score=0.5)]

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "rerank", fake_rerank)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    results = await pl.run_pipeline(
        context="浮点数比较的坑",
        search_query="浮点 比较",
        pg=FakePG(),
        qdrant=None,
        embedding_config=EmbeddingConfig(),
        max_results=6,
    )

    boosted_pattern_score = 0.5 + pl._condition_boost()
    expected = compute_score(e1, boosted_pattern_score, rel_boost=0.0, urgency=0.0)
    assert results[0].score == pytest.approx(expected)
