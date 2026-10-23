"""LLM Client — thin wrapper for OpenAI-compatible chat completion API.

Used by learning modules (mutation, crossover, genesis) for cold-path evolution.
Falls back gracefully: if not configured or call fails, returns None.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

import httpx

from app.config import EmbeddingConfig, LLMConfig

logger = logging.getLogger(__name__)


_config: LLMConfig | None = None
_embedding_config: EmbeddingConfig | None = None


def set_default_configs(
    llm_config: LLMConfig | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> None:
    """Set process-wide defaults loaded from DB-managed runtime settings."""
    global _config, _embedding_config
    if llm_config is not None:
        _config = llm_config
    if embedding_config is not None:
        if _embedding_config != embedding_config:
            _embed_cache.clear()
        _embedding_config = embedding_config


def get_llm_config() -> LLMConfig:
    global _config
    if _config is None:
        _config = LLMConfig(
            api_key=os.getenv("CE_LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            api_url=os.getenv("CE_LLM_API_URL", os.getenv("OPENAI_API_URL", "https://api.openai.com/v1")),
            model=os.getenv("CE_LLM_MODEL", "gpt-4o-mini"),
            temperature=float(os.getenv("CE_LLM_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("CE_LLM_MAX_TOKENS", "1024")),
        )
    return _config



def anthropic_url(base: str) -> str:
    """Anthropic Messages 接口地址:根地址 / .../v1 / 完整 .../messages 三种写法都接受。"""
    base = (base or "").rstrip("/")
    if base.endswith("/messages"):
        return base
    return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"


_chat_client: httpx.AsyncClient | None = None


def _get_chat_client() -> httpx.AsyncClient:
    global _chat_client
    if _chat_client is None or _chat_client.is_closed:
        _chat_client = httpx.AsyncClient(timeout=60.0)
    return _chat_client


async def complete(
    cfg: LLMConfig,
    messages: list[dict],
    *,
    system: str = "",
    temperature: float | None = None,
    max_tokens: int | None = None,
    extra: dict | None = None,
    timeout: float = 30.0,
    raise_errors: bool = False,
) -> str | None:
    """一次对话补全(OpenAI Chat Completions 或 Anthropic Messages,按 cfg.protocol)。

    返回正文(思考块忽略);失败返回 None,raise_errors=True 时抛出(连通性测试要看到原因)。"""
    body_extra = {**cfg.extra, **(extra or {})}
    try:
        if cfg.protocol == "anthropic":
            body = {"model": cfg.model, "max_tokens": max_tokens or cfg.max_tokens, "messages": messages,
                    **body_extra}
            if system:
                body["system"] = system
            if temperature is not None and "thinking" not in body_extra:   # 开思考时 Claude 只接受默认温度
                body.setdefault("temperature", temperature)
            headers = {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01",
                       "Content-Type": "application/json", **cfg.headers}
            resp = await _get_chat_client().post(anthropic_url(cfg.api_url), headers=headers, json=body,
                                                 timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("stop_reason") == "max_tokens":
                _warn_truncated(cfg, body["max_tokens"], None)
            return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
        body = {"model": cfg.model, "messages": msgs, "max_tokens": max_tokens or cfg.max_tokens}
        if temperature is not None:
            body["temperature"] = temperature
        body.update(body_extra)
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json", **cfg.headers}
        resp = await _get_chat_client().post(f"{cfg.api_url.rstrip('/')}/chat/completions", headers=headers,
                                             json=body, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        if data["choices"][0].get("finish_reason") == "length":
            usage = data.get("usage") or {}
            _warn_truncated(cfg, body["max_tokens"], (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        return data["choices"][0]["message"].get("content") or ""
    except Exception as e:
        if raise_errors:
            raise
        logger.warning("LLM call failed (%s %s): %s", cfg.protocol, cfg.model, e)
        return None


def _warn_truncated(cfg: LLMConfig, max_tokens: int, reasoning: int | None) -> None:
    """输出写到 max_tokens 被截断:JSON 类调用会因此解析失败。思考模型的思考也占这个额度。"""
    logger.warning("LLM output truncated at max_tokens=%d (%s, reasoning tokens: %s) — raise this model's "
                   "max_tokens on the 模型 page", max_tokens, cfg.model, reasoning if reasoning is not None else "n/a")


async def chat(
    system: str,
    user: str,
    config: LLMConfig | None = None,
    temperature: float | None = None,
    timeout: float = 30.0,
) -> str | None:
    """Send a chat completion request. Returns response text or None on failure."""
    cfg = config or get_llm_config()
    if not cfg.available:
        logger.debug("LLM not configured, skipping")
        return None
    text = await complete(
        cfg, [{"role": "user", "content": user}], system=system,
        temperature=temperature if temperature is not None else cfg.temperature, timeout=cfg.timeout or timeout,
    )
    return text.strip() if text is not None else None


async def chat_json(
    system: str,
    user: str,
    config: LLMConfig | None = None,
    timeout: float = 30.0,
) -> dict | list | None:
    """Chat completion that expects JSON output. Returns parsed JSON or None."""
    result = await chat(
        system=system + "\n\n回复必须是合法的 JSON，不要包含 markdown 代码块标记。",
        user=user,
        config=config,
        temperature=0.5,  # Lower temperature for structured output
        timeout=timeout,
    )
    if not result:
        return None

    # Strip markdown code block if present
    text = result.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("LLM returned invalid JSON: %s...", text[:100])
        return None


# ── Embedding ─────────────────────────────────────────────────

_embed_cache: dict[str, list[float]] = {}
_embed_client: httpx.AsyncClient | None = None


def _get_embed_client() -> httpx.AsyncClient:
    """Lazy singleton httpx client for embedding calls."""
    global _embed_client
    if _embed_client is None or _embed_client.is_closed:
        _embed_client = httpx.AsyncClient(timeout=30)
    return _embed_client


async def embed(text: str, config: "EmbeddingConfig | None" = None) -> list[float] | None:
    """Generate embedding vector for text via OpenAI-compatible API.

    Returns vector or None if not configured / call fails.
    Uses in-memory cache to avoid redundant calls.
    """
    if config is None:
        cfg = _embedding_config or EmbeddingConfig(
            api_key=os.getenv("EMBEDDING_API_KEY", ""),
            api_url=os.getenv("EMBEDDING_API_URL", ""),
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
        )
    else:
        cfg = config

    if not cfg.api_key or not cfg.api_url:
        return None

    # Cache hit。键 = 实际请求体(text[:8000],见 _embed_request)的全文哈希:
    # 前缀截断键会让"前 N 字符相同、后文不同"的文本错拿同一向量——查询侧
    # 返回错误召回,文档侧(sync)更会把错误向量持久化进 Qdrant。
    cache_identity = (
        f"{cfg.api_url.rstrip('/')}\0{cfg.model}\0{cfg.dimensions}\0{text[:8000]}"
    )
    cache_key = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
    if cache_key in _embed_cache:
        return _embed_cache[cache_key]

    # Normalize URL: ensure it ends with /embeddings
    url = cfg.api_url.rstrip("/")
    if not url.endswith("/embeddings"):
        url = url + "/embeddings"

    vector, transient = await _embed_request(url, cfg, text)
    if vector is None and transient:
        # 瞬时失败(如 SiliconFlow 502/超时)重试一次,避免把噪声误判成真实召回缺失;
        # 4xx 配置类错误(如鉴权失败)不重试,重试也必定再次失败
        await asyncio.sleep(1.0)
        vector, _ = await _embed_request(url, cfg, text)
    if vector is None:
        return None

    # Cache with LRU-style eviction (drop oldest entries)
    if len(_embed_cache) >= 500:
        # Remove oldest 100 entries
        keys_to_remove = list(_embed_cache.keys())[:100]
        for k in keys_to_remove:
            del _embed_cache[k]
    _embed_cache[cache_key] = vector
    return vector


async def _embed_request(
    url: str, cfg: EmbeddingConfig, text: str
) -> tuple[list[float] | None, bool]:
    """单次 embedding HTTP 请求。返回 (vector, transient)：失败时 vector 为 None,
    transient 标记该失败是否值得重试——超时/连接类错误、429 限流与 5xx 视为瞬时故障,
    其余 4xx（鉴权/参数等配置错误）重试也必然再次失败，不应重试。
    """
    try:
        client = _get_embed_client()
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            json={
                "model": cfg.model,
                "input": text[:8000],  # Truncate to avoid token limits
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"], False
    except httpx.HTTPStatusError as e:
        logger.warning("Embedding failed: %s", e)
        # 429 限流也是瞬时的(与 rerank 一致):不重试的话,批量同步时会静默丢掉该条向量
        code = e.response.status_code
        return None, code == 429 or code >= 500
    except httpx.TransportError as e:
        # 超时(TimeoutException)、连接失败等网络层瞬时故障,值得重试
        logger.warning("Embedding failed: %s", e)
        return None, True
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return None, False
