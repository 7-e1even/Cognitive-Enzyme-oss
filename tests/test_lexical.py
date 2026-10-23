"""词面召回(BM25 稀疏向量)单测 — 纯内存替身。"""
import pytest

from app.core import Enzyme, Pattern
from app.matching import lexical
from app.matching import pipeline as pl


def test_tokenize_mixed_language():
    toks = lexical.tokenize("The SQL injection in login.php 导致数据库泄露")
    assert "sql" in toks and "injection" in toks and "login.php" in toks
    assert "the" not in toks and "in" not in toks
    assert {"导致", "数据", "据库", "泄露"} <= set(toks)


def test_vectors_bm25_shape():
    ids, vals = lexical.doc_vector("buffer overflow overflow in parser")
    assert len(ids) == len(vals) == 3 and ids == sorted(ids)
    tf = dict(zip(ids, vals))
    assert tf[lexical._term_id("overflow")] > tf[lexical._term_id("buffer")]   # 词频饱和,但重复词更重
    qids, qvals = lexical.query_vector("overflow overflow parser")
    assert qvals == [1.0, 1.0] and len(qids) == 2
    assert lexical.doc_vector("the of and") is None and lexical.query_vector("") is None


def test_merge_lexical_rank_aligned():
    dense = [{"id": "a", "_score": 0.8}, {"id": "b", "_score": 0.7}, {"id": "c", "_score": 0.6}]
    lex = [{"id": "x", "_score": 17.0}, {"id": "c", "_score": 9.0}]
    merged = lexical.merge_lexical(dense, lex)
    # x 是 BM25 第 1 名 → 记为向量第 1 名的分数;c 在 BM25 第 2 名 → 0.7 > 原 0.6
    assert [h["id"] for h in merged][:2] in (["a", "x"], ["x", "a"])
    assert {h["id"]: h["_score"] for h in merged}["c"] == 0.7
    assert lexical.merge_lexical(dense, []) == dense
    assert [h["id"] for h in lexical.merge_lexical([], lex)] == ["x", "c"]


class _PG:
    def __init__(self, *es):
        self.e = {e.id: e for e in es}

    async def get_enzymes(self, ids):
        return [self.e[i] for i in ids if i in self.e]


class _Q:
    async def search(self, vector, domain=None, limit=50):
        return [{"id": "dense", "_score": 0.5}]

    async def search_lexical(self, text, domain=None, limit=10):
        return [{"id": "lex", "_score": 12.0}]

    async def search_exemplars(self, vector, domain=None, limit=30):
        return []


def _e(eid):
    return Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}")


@pytest.mark.asyncio
async def test_recall_merges_lexical_by_default(monkeypatch):
    async def fake_embed(text, config=None):
        return [0.1]
    monkeypatch.setattr("app.llm_client.embed", fake_embed)
    pg = _PG(_e("dense"), _e("lex"))
    trace = {}
    got = await pl._recall("q", "ctx", pg, _Q(), None, None, trace=trace)
    assert {e.id for e in got} == {"dense", "lex"} and trace["lexical_ids"] == ["lex"]   # 默认开
    monkeypatch.setenv("CE_LEXICAL_RECALL", "off")
    got = await pl._recall("q", "ctx", pg, _Q(), None, None)
    assert [e.id for e in got] == ["dense"]


@pytest.mark.asyncio
async def test_recall_lexical_only_when_embedding_down(monkeypatch):
    async def no_embed(text, config=None):
        return None
    monkeypatch.setattr("app.llm_client.embed", no_embed)
    monkeypatch.setenv("CE_LEXICAL_RECALL", "on")
    got = await pl._recall("q", "ctx", _PG(_e("lex")), _Q(), None, None)
    assert [e.id for e in got] == ["lex"]


@pytest.mark.asyncio
async def test_sync_maintains_lexical_index():
    """词面索引是派生数据:同步、删除、回填都跟着走;写失败不影响主流程。"""
    from app.storage.sync import SyncManager

    class Q:
        def __init__(self):
            self.lex, self.deleted = [], []

        async def upsert_lexical(self, enzymes):
            self.lex += [e.id for e in enzymes]

        async def delete_lexical(self, eid):
            self.deleted.append(eid)

    class PG:
        async def list_all_enzymes(self):
            return [_e(f"e{i}") for i in range(300)]

    q = Q()
    sm = SyncManager(PG(), q)
    assert await sm.backfill_lexical() == 300 and len(q.lex) == 300

    class Broken(Q):
        async def upsert_lexical(self, enzymes):
            raise RuntimeError("sparse vectors unsupported")
    await SyncManager(PG(), Broken()).sync_lexical([_e("x")])          # 只记警告,不抛
