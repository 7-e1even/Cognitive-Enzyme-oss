"""语义去重(app/learning/dedup.py)单测 — 纯内存,embed / Qdrant / Jev 全部替身。"""
import pytest

from app.config import TypeSafeConfig
from app.core import Enzyme, Pattern
from app.learning import dedup

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")


class FakePG:
    def __init__(self, enzymes):
        self.e = {x.id: x for x in enzymes}
        self.events, self.relations, self.deleted = [], [], []
        self.verdicts: dict = {}   # 判定缓存(PGStore 的 dedup_verdicts 表的内存版)

    async def list_enzymes(self, limit=10000):
        return list(self.e.values())

    async def upsert_enzyme(self, enzyme, expected_version=None):
        self.e[enzyme.id] = enzyme
        return 1

    async def upsert_relation(self, rel):
        self.relations.append(rel)

    async def log_event(self, ev):
        self.events.append(ev)

    async def delete_enzyme(self, eid):
        self.deleted.append(eid)
        self.e.pop(eid, None)

    async def get_dedup_verdicts(self, keys):
        return {k: self.verdicts[k] for k in keys if k in self.verdicts}

    async def save_dedup_verdicts(self, rows):
        for key, h, p in rows:
            self.verdicts[key] = (h, p)

    async def prune_dedup_verdicts(self, keep):
        for key in set(self.verdicts) - set(keep):
            del self.verdicts[key]


class FakeSync:
    async def sync_enzyme(self, e, generation=None):
        pass

    async def sync_delete(self, eid):
        pass


class FakeQdrant:
    embedding_config = None

    def __init__(self, neighbors):
        self.neighbors = neighbors

    async def search(self, vector, limit=4, include_all=False):
        return [{"id": x} for x in [vector[0]] + self.neighbors.get(vector[0], [])]


def _e(eid, conf=0.5, trig=0, succ=0, then=None, patterns=("x",)):
    return Enzyme(id=eid, patterns=[Pattern(value=v) for v in patterns], then=then or f"then-{eid}",
                  confidence=conf, trigger_count=trig, success_count=succ)


@pytest.fixture
def wired(monkeypatch):
    async def fake_embed(text, config=None):
        return [text.split(" ")[-2]]  # _enzyme_text 以 "<patterns> <then> <because>" 拼接;then=id 标记

    monkeypatch.setattr(dedup, "embed", fake_embed)
    monkeypatch.setattr(dedup, "_enzyme_text", lambda e: f"p {e.id} ")

    def setup(enzymes, neighbors, probs):
        async def judge(a, b, cfg):
            return probs.get(tuple(sorted((a.id, b.id))))
        monkeypatch.setattr(dedup, "judge_duplicate", judge)
        return FakePG(enzymes), FakeQdrant(neighbors)
    return setup


@pytest.mark.asyncio
async def test_merges_above_tau_keeps_stronger_and_sums_evidence(wired):
    a = _e("a", conf=0.8, trig=4, succ=3, patterns=("浮点 比较",))
    b = _e("b", conf=0.4, trig=2, succ=1, patterns=("double ==",))
    b.exemplars = ["b 见过的真实情境"]
    c = _e("c")
    pg, qd = wired([a, b, c], {"a": ["b", "c"], "b": ["a"], "c": ["a"]},
                   {("a", "b"): 0.95, ("a", "c"): 0.3})
    out = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert out["merged"] == 1 and pg.deleted == ["b"]
    keeper = pg.e["a"]
    assert {p.value for p in keeper.patterns} == {"浮点 比较", "double =="}
    assert (keeper.trigger_count, keeper.success_count) == (6, 4) and abs(keeper.precision - 4 / 6) < 1e-9
    assert keeper.exemplars == ["b 见过的真实情境"]
    snap = pg.events[0].detail
    assert snap["absorbed_id"] == "b" and snap["absorbed_snapshot"]["then"] == "then-b"


@pytest.mark.asyncio
async def test_each_enzyme_merges_at_most_once_greedy_by_p(wired):
    es = [_e("a", conf=0.9), _e("b", conf=0.5), _e("c", conf=0.6)]
    pg, qd = wired(es, {"a": ["b", "c"], "b": ["c"]},
                   {("a", "b"): 0.85, ("b", "c"): 0.97, ("a", "c"): 0.9})
    out = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert [(m["keeper"], m["absorbed"]) for m in out["pairs"]] == [("c", "b")]  # 0.97 先合,a 本轮不再参与


@pytest.mark.asyncio
async def test_below_tau_or_judge_failure_no_merge(wired):
    pg, qd = wired([_e("a"), _e("b"), _e("c")], {"a": ["b", "c"]}, {("a", "b"): 0.79, ("a", "c"): None})
    out = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert out["merged"] == 0 and pg.deleted == [] and out["judged"] == 1


@pytest.mark.asyncio
async def test_skipped_without_key_or_when_disabled(wired, monkeypatch):
    pg, qd = wired([_e("a"), _e("b")], {"a": ["b"]}, {("a", "b"): 0.99})
    assert (await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TypeSafeConfig()))["skipped"]
    monkeypatch.setenv("CE_DEDUP_JEV", "off")
    assert (await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS))["skipped"]
    assert pg.deleted == []


# ── 判定缓存:省 Jev 调用,结果必须和原来一模一样 ──────────────────────────────

@pytest.fixture
def counting(monkeypatch):
    """embed / Jev 调用计数;向量 = [id],近邻表决定检索结果。"""
    calls = {"embed": 0, "judge": 0}

    async def fake_embed(text, config=None):
        calls["embed"] += 1
        return [text.split(" ")[1]]

    monkeypatch.setattr(dedup, "embed", fake_embed)
    monkeypatch.setattr(dedup, "_enzyme_text", lambda e: f"p {e.id} ")

    def setup(enzymes, neighbors, probs):
        async def judge(a, b, cfg):
            calls["judge"] += 1
            return probs.get(tuple(sorted((a.id, b.id))))
        monkeypatch.setattr(dedup, "judge_duplicate", judge)
        return FakePG(enzymes), FakeQdrant(neighbors), calls
    return setup


@pytest.mark.asyncio
async def test_verdicts_reused_until_content_changes(counting):
    es = [_e("a"), _e("b"), _e("c")]
    pg, qd, calls = counting(es, {"a": ["b", "c"]}, {("a", "b"): 0.3, ("a", "c"): 0.4})
    first = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert calls["judge"] == 2 and first["reused_verdicts"] == 0

    second = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert calls["judge"] == 2 and second["reused_verdicts"] == 2          # 没变:一次 Jev 都不调
    assert (second["candidates"], second["judged"], second["merged"]) == (first["candidates"], first["judged"], first["merged"])

    pg.e["c"].then = "改过的建议"
    third = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert calls["judge"] == 3 and third["reused_verdicts"] == 1           # 只重判涉及 c 的那一对


@pytest.mark.asyncio
async def test_cached_verdict_still_merges_like_a_fresh_one(counting):
    es = [_e("a", conf=0.9), _e("b", conf=0.4), _e("c")]
    pg, qd, calls = counting(es, {"a": ["b", "c"]}, {("a", "b"): 0.95, ("a", "c"): 0.2})
    key = dedup._pair_key(("a", "b"))
    await pg.save_dedup_verdicts([(key, dedup._verdict_hash(es[0], es[1], TS), 0.95)])
    out = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert out["reused_verdicts"] == 1 and calls["judge"] == 1 and pg.deleted == ["b"]


@pytest.mark.asyncio
async def test_failed_judgement_is_not_cached(counting):
    es = [_e("a"), _e("b")]
    pg, qd, calls = counting(es, {"a": ["b"]}, {("a", "b"): None})
    await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert calls["judge"] == 2   # 判失败的下次照样重判


@pytest.mark.asyncio
async def test_borderline_verdicts_are_re_judged_every_run(counting):
    """Jev 重判有小抖动:离阈值(默认 0.8)不到 0.05 的判定不复用,每次照旧重判。"""
    es = [_e("a"), _e("b"), _e("c")]
    pg, qd, calls = counting(es, {"a": ["b", "c"]}, {("a", "b"): 0.78, ("a", "c"): 0.1})
    await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    second = await dedup.merge_semantic_duplicates(pg, qd, FakeSync(), TS)
    assert calls["judge"] == 3 and second["reused_verdicts"] == 1   # 0.78 重判,0.1 复用
