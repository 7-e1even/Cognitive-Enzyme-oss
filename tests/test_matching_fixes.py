"""2026-07-07 匹配链路健壮性修复的行为锁定:
- embed 缓存键全文哈希(前缀碰撞不再共享向量)
- rerank 瞬时失败重试一次、永久失败不重试
- rerank 回退时 trace 标记 rerank_failed、成功时落 rerank_raw
"""
import pytest

from app.config import EmbeddingConfig
from app.core import Enzyme, Pattern
from app.matching import pipeline as pl
from app.matching import rerank as rr


class FakePG:
    async def get_related_batch(self, enzyme_ids, limit=5):
        return {eid: [] for eid in enzyme_ids}


def _enzyme(eid: str) -> Enzyme:
    return Enzyme(
        id=eid,
        patterns=[Pattern(value="浮点")],
        then="浮点比较用容差",
        because="精度误差",
    )


# ── embed 缓存键 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_embed_cache_key_no_prefix_collision(monkeypatch):
    """前 200 字符相同、后文不同的两条文本必须各自发请求——
    旧键 text[:200] 会让第二条错拿第一条的向量。"""
    import app.llm_client as lc

    calls: list[str] = []

    async def fake_request(url, cfg, text):
        calls.append(text)
        return [1.0], False

    monkeypatch.setattr(lc, "_embed_request", fake_request)
    lc._embed_cache.clear()
    cfg = EmbeddingConfig(api_key="k", api_url="http://x")

    prefix = "x" * 250
    await lc.embed(prefix + "AAA", config=cfg)
    await lc.embed(prefix + "BBB", config=cfg)
    assert len(calls) == 2  # 旧键下这里会是 1(第二条命中错误缓存)

    await lc.embed(prefix + "AAA", config=cfg)  # 全文相同 → 命中缓存
    assert len(calls) == 2
    lc._embed_cache.clear()


@pytest.mark.asyncio
async def test_embed_cache_isolated_by_model(monkeypatch):
    import app.llm_client as lc

    calls: list[str] = []

    async def fake_request(url, cfg, text):
        calls.append(cfg.model)
        return [float(len(calls))], False

    monkeypatch.setattr(lc, "_embed_request", fake_request)
    lc._embed_cache.clear()
    one = EmbeddingConfig(api_key="k", api_url="http://x", model="model-a")
    two = EmbeddingConfig(api_key="k", api_url="http://x", model="model-b")

    assert await lc.embed("same text", config=one) == [1.0]
    assert await lc.embed("same text", config=two) == [2.0]
    assert calls == ["model-a", "model-b"]
    lc._embed_cache.clear()


# ── rerank 重试 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerank_retries_transient_once(monkeypatch):
    attempts: list[int] = []

    async def fake_request(url, headers, payload):
        attempts.append(1)
        if len(attempts) == 1:
            return None, True, None  # 首次 429/5xx/超时
        return [rr.RerankResult(index=0, score=0.9)], False, None

    monkeypatch.setattr(rr, "_rerank_request", fake_request)
    monkeypatch.setattr(rr, "_RETRY_BACKOFF_CAP", 0.0)  # 测试免等
    cfg = EmbeddingConfig(api_key="k", rerank_api_url="http://x")

    out = await rr.rerank("q", ["doc"], cfg)
    assert len(attempts) == 2
    assert out and out[0].score == 0.9


@pytest.mark.asyncio
async def test_rerank_retry_budget_configurable(monkeypatch):
    """批量评测撞限流:CE_RERANK_RETRIES 放宽重试次数(指数退避),默认仍只重试 1 次。"""
    attempts: list[float] = []
    sleeps: list[float] = []

    async def rate_limited(url, headers, payload):
        attempts.append(1)
        if len(attempts) <= 3:
            return None, True, None  # 连续 3 次 429
        return [rr.RerankResult(index=0, score=0.7)], False, None

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(rr, "_rerank_request", rate_limited)
    monkeypatch.setattr(rr.asyncio, "sleep", fake_sleep)
    cfg = EmbeddingConfig(api_key="k", rerank_api_url="http://x")

    assert await rr.rerank("q", ["doc"], cfg) is None          # 默认:1 次重试后放弃
    assert len(attempts) == 2 and sleeps == [1.0]

    attempts.clear(); sleeps.clear()
    monkeypatch.setenv("CE_RERANK_RETRIES", "4")
    monkeypatch.setenv("CE_RERANK_BACKOFF_CAP", "30")
    out = await rr.rerank("q", ["doc"], cfg)
    assert out and out[0].score == 0.7
    assert len(attempts) == 4 and sleeps == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_rerank_no_retry_on_permanent_failure(monkeypatch):
    attempts: list[int] = []

    async def fake_request(url, headers, payload):
        attempts.append(1)
        return None, False, None  # 4xx 配置类错误

    monkeypatch.setattr(rr, "_rerank_request", fake_request)
    cfg = EmbeddingConfig(api_key="k", rerank_api_url="http://x")

    assert await rr.rerank("q", ["doc"], cfg) is None
    assert len(attempts) == 1


# ── pipeline trace: rerank_failed / rerank_raw ─────────────────

@pytest.mark.asyncio
async def test_trace_marks_rerank_failed_on_fallback(monkeypatch):
    e1 = _enzyme("e1")

    async def fake_recall(*args, **kwargs):
        return [e1]

    async def failing_rerank(query, documents, config, top_n=None):
        return None  # 重试后仍失败 → 回退 pattern 打分

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "rerank", failing_rerank)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    trace: dict = {}
    results = await pl.run_pipeline(
        context="浮点数比较的坑", search_query="浮点",
        pg=FakePG(), qdrant=None,
        embedding_config=EmbeddingConfig(), max_results=6, trace=trace,
    )
    assert trace["rerank_failed"] is True
    assert trace["reranked_ids"] == ["e1"]  # 回退保留 Stage3 结果
    assert "rerank_raw" not in trace        # 没有 rerank 分数可记
    assert results  # 回退路径照常出结果(graceful fallback)


@pytest.mark.asyncio
async def test_trace_rerank_raw_recorded_before_floor(monkeypatch):
    e1 = _enzyme("e1")

    async def fake_recall(*args, **kwargs):
        return [e1]

    async def fake_rerank(query, documents, config, top_n=None):
        return [rr.RerankResult(index=0, score=0.42)]

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "rerank", fake_rerank)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    trace: dict = {}
    await pl.run_pipeline(
        context="浮点数比较的坑", search_query="浮点",
        pg=FakePG(), qdrant=None,
        embedding_config=EmbeddingConfig(), max_results=6, trace=trace,
    )
    assert trace["rerank_failed"] is False
    assert trace["rerank_raw"] == [("e1", 0.42)]


@pytest.mark.asyncio
async def test_trace_rerank_not_attempted_is_not_failure(monkeypatch):
    e1 = _enzyme("e1")

    async def fake_recall(*args, **kwargs):
        return [e1]

    monkeypatch.setattr(pl, "_recall", fake_recall)
    monkeypatch.setattr(pl, "_EXPLORATION_RATE", 0.0)

    trace: dict = {}
    await pl.run_pipeline(
        context="浮点数比较的坑", search_query="浮点",
        pg=FakePG(), qdrant=None,
        embedding_config=None, max_results=6, trace=trace,  # 未配置 → 未尝试
    )
    assert trace["rerank_failed"] is False


@pytest.mark.asyncio
async def test_embed_retries_rate_limit_but_not_auth_error(monkeypatch):
    """429 限流是瞬时的:重试一次拿到向量;401 是配置错误,不重试。"""
    import httpx
    import app.llm_client as lc

    async def no_sleep(_):
        return None

    def serve(codes):
        seen = []

        def handler(request):
            seen.append(request)
            code = codes[min(len(seen) - 1, len(codes) - 1)]
            if code != 200:
                return httpx.Response(code, json={"error": "x"})
            return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.25]}]})
        return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(lc.asyncio, "sleep", no_sleep)
    cfg = EmbeddingConfig(api_key="k", api_url="http://x")

    seen, client = serve([429, 200])
    monkeypatch.setattr(lc, "_get_embed_client", lambda: client)
    lc._embed_cache.clear()
    assert await lc.embed("rate limited once", config=cfg) == [0.5, 0.25]
    assert len(seen) == 2

    seen, client = serve([401, 200])
    monkeypatch.setattr(lc, "_get_embed_client", lambda: client)
    lc._embed_cache.clear()
    assert await lc.embed("bad key", config=cfg) is None
    assert len(seen) == 1
    lc._embed_cache.clear()
