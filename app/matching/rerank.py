"""Rerank — SiliconFlow rerank API client.

Calls POST https://api.siliconflow.cn/v1/rerank to re-order candidates
by semantic relevance. Graceful fallback on failure.

瞬时失败(429/5xx/超时/连接)重试一次:rerank 挂在 prime 热路径上(hook 端
超时预算有限),重试封顶 1 次 + 短退避,避免延迟无界放大;4xx 配置类错误
重试也必然再失败,不重试。失败回退 pattern 打分由 pipeline 兜底——注意
该回退会让 relevance floor 门失效(pipeline 有 trace 标记),所以这里每
少失败一次,静默纪律就多保住一次。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app import settings
from app.config import EmbeddingConfig

logger = logging.getLogger(__name__)

_RETRY_BACKOFF_CAP = 2.0  # 秒;Retry-After 超过此值不值得在热路径上等


def _retry_policy() -> tuple[int, float]:
    """(重试次数, 单次退避上限秒)。热路径默认只重试 1 次、最多等 2 秒;批量评测 / 回放撞上远端
    限流(429)时放宽 ranking.rerank_retries / ranking.rerank_backoff_cap(指数退避),换精排不失败。"""
    return settings.get("ranking.rerank_retries"), settings.get("ranking.rerank_backoff_cap")


@dataclass
class RerankResult:
    index: int
    score: float


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy singleton httpx client（与 llm_client 的 embed 客户端同款模式）。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def rerank(
    query: str,
    documents: list[str],
    config: EmbeddingConfig,
    top_n: int | None = None,
) -> list[RerankResult] | None:
    """Call SiliconFlow rerank API. Returns sorted results or None on failure.

    Args:
        query: The user context (what to match against).
        documents: List of document texts to rerank.
        config: EmbeddingConfig with api_key, api_url, rerank_model.
        top_n: Max results to return. Defaults to config.rerank_top_n.
    """
    api_key = config.rerank_api_key or config.api_key
    if not api_key or not config.rerank_api_url:
        return None

    if not documents:
        return None

    top_n = top_n or config.rerank_top_n

    url = config.rerank_api_url.rstrip("/")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.rerank_model,
        "query": query,
        "documents": documents,
        "top_n": top_n,
        "return_documents": False,
    }

    results, transient, retry_after = await _rerank_request(url, headers, payload)
    retries, cap = _retry_policy()
    for attempt in range(retries):
        if results is not None or not transient:
            break
        await asyncio.sleep(min(retry_after or 2.0 ** attempt, cap))
        results, transient, retry_after = await _rerank_request(url, headers, payload)
    return results


async def _rerank_request(
    url: str, headers: dict, payload: dict
) -> tuple[list[RerankResult] | None, bool, float | None]:
    """单次 rerank HTTP 请求。返回 (results, transient, retry_after):
    失败时 results 为 None,transient 标记是否值得重试(429/5xx/超时/连接),
    retry_after 为服务端建议的退避秒数(仅 429 且响应头存在时)。"""
    try:
        client = _get_client()
        # ranking.rerank_timeout:按请求传,后台改了即时生效
        resp = await client.post(url, headers=headers, json=payload, timeout=settings.get("ranking.rerank_timeout"))

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after: float | None = None
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                pass
            logger.warning("Rerank API transient failure: HTTP %s", resp.status_code)
            return None, True, retry_after

        resp.raise_for_status()
        data = resp.json()

        results = [
            RerankResult(index=item["index"], score=item["relevance_score"])
            for item in data.get("results", [])
        ]
        results.sort(key=lambda r: r.score, reverse=True)

        logger.info(
            "Rerank: %d docs → top %d (model=%s)",
            len(payload["documents"]), len(results), payload["model"],
        )
        return results, False, None

    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("Rerank API transient failure: %s", e)
        return None, True, None
    except Exception as e:
        logger.warning("Rerank API call failed, falling back: %s", e)
        return None, False, None
