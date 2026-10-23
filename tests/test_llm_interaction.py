"""llm_client.complete:同一接口说 OpenAI Chat Completions 与 Anthropic Messages 两种协议。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm_client
from app.config import LLMConfig


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Client:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.is_closed = False

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        return _Resp(self.data)


@pytest.mark.asyncio
async def test_openai_protocol_payload(monkeypatch):
    client = _Client({"choices": [{"message": {"content": "pong"}}]})
    monkeypatch.setattr(llm_client, "_chat_client", client)
    cfg = LLMConfig(api_key="k", api_url="https://example.test/v1/", model="m", extra={"thinking": {"type": "disabled"}},
                    headers={"X-Title": "ce"})
    text = await llm_client.complete(cfg, [{"role": "user", "content": "ping"}], system="sys", temperature=0,
                                     max_tokens=10)
    assert text == "pong"
    url, headers, body = client.calls[0]
    assert url == "https://example.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k" and headers["X-Title"] == "ce"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["temperature"] == 0 and body["max_tokens"] == 10 and body["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_anthropic_protocol_payload(monkeypatch):
    client = _Client({"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "pong"}]})
    monkeypatch.setattr(llm_client, "_chat_client", client)
    cfg = LLMConfig(api_key="sk-ant", api_url="https://api.anthropic.com", model="claude", protocol="anthropic")
    text = await llm_client.complete(cfg, [{"role": "user", "content": "ping"}], system="sys", temperature=0.2)
    assert text == "pong"                                   # 思考块忽略,只取 text
    url, headers, body = client.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant" and "anthropic-version" in headers
    assert body["system"] == "sys" and body["temperature"] == 0.2


@pytest.mark.asyncio
async def test_none_temperature_is_not_sent(monkeypatch):
    client = _Client({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llm_client, "_chat_client", client)
    cfg = LLMConfig(api_key="k", api_url="https://x/v1", temperature=None)
    assert await llm_client.chat("s", "u", config=cfg) == "ok"
    assert "temperature" not in client.calls[0][2]


@pytest.mark.asyncio
async def test_failure_returns_none_or_raises(monkeypatch):
    class Boom(_Client):
        async def post(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(llm_client, "_chat_client", Boom({}))
    cfg = LLMConfig(api_key="k", api_url="https://x/v1")
    assert await llm_client.complete(cfg, [{"role": "user", "content": "x"}]) is None
    with pytest.raises(RuntimeError):
        await llm_client.complete(cfg, [{"role": "user", "content": "x"}], raise_errors=True)


@pytest.mark.asyncio
async def test_chat_skips_when_unconfigured():
    assert await llm_client.chat("s", "u", config=LLMConfig()) is None


class _TimedClient(_Client):
    async def post(self, url, headers=None, json=None, timeout=None):
        self.timeout = timeout
        return await super().post(url, headers=headers, json=json, timeout=timeout)


@pytest.mark.asyncio
async def test_chat_uses_model_timeout_when_set(monkeypatch):
    client = _TimedClient({"choices": [{"message": {"content": "ok"}}]})
    monkeypatch.setattr(llm_client, "_chat_client", client)
    await llm_client.chat("s", "u", config=LLMConfig(api_key="k", api_url="https://x/v1"))
    assert client.timeout == 30.0                      # 没设:用调用处的默认值
    await llm_client.chat("s", "u", config=LLMConfig(api_key="k", api_url="https://x/v1", timeout=90))
    assert client.timeout == 90                        # 模型页设了:思考模型留够时间


@pytest.mark.asyncio
async def test_truncated_output_is_reported(monkeypatch, caplog):
    client = _Client({"choices": [{"finish_reason": "length", "message": {"content": "[{\"trigger\": \""}}],
                      "usage": {"completion_tokens_details": {"reasoning_tokens": 1008}}})
    monkeypatch.setattr(llm_client, "_chat_client", client)
    cfg = LLMConfig(api_key="k", api_url="https://x/v1", model="glm-5.3")
    with caplog.at_level("WARNING"):
        assert await llm_client.chat_json("s", "u", config=cfg) is None
    assert "truncated at max_tokens=1024" in caplog.text and "reasoning tokens: 1008" in caplog.text
