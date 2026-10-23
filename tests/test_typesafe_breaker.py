"""TypeSafe 熔断:鉴权 / 计费失败后暂停调用;key 正常时行为不变,换 key 立刻恢复。"""

import httpx
import pytest

from app import typesafe_client as ts
from app.config import TypeSafeConfig

QUESTIONS = {"q": {"type": "noul", "instructions": "x", "criteria": {"true": "a", "false": "b"}}}


@pytest.fixture
def api(monkeypatch):
    """假的 TypeSafe:按 key 决定返回什么,记下每次真正打出去的请求。"""
    hits: list[str] = []
    replies = {"good": 200, "broke": 402, "denied": 403, "flaky": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["Authorization"].removeprefix("Bearer ")
        hits.append(key)
        status = replies[key]
        if status == 200:
            return httpx.Response(200, json={"answers": {"q": {"noul": 0.7}}, "model": "jev"})
        return httpx.Response(status, json={"detail": {"error_type": "billing_error"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ts, "_get_client", lambda: client)
    monkeypatch.setattr(ts, "_breaker", {})
    return hits


def _cfg(key: str) -> TypeSafeConfig:
    return TypeSafeConfig(api_key=key, api_url="https://ts.invalid/v1")


@pytest.mark.asyncio
async def test_working_key_is_never_blocked(api):
    for _ in range(5):
        assert (await ts.system_one("s", QUESTIONS, _cfg("good")))["answers"]["q"]["noul"] == 0.7
    assert len(api) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["broke", "denied"])
async def test_auth_or_billing_failure_pauses_calls(api, key):
    for _ in range(20):
        assert await ts.system_one("s", QUESTIONS, _cfg(key)) is None
    assert len(api) == 1          # 只打了第一次,之后都在冷却期内直接回退


@pytest.mark.asyncio
async def test_new_key_recovers_immediately(api):
    assert await ts.system_one("s", QUESTIONS, _cfg("broke")) is None
    assert await ts.system_one("s", QUESTIONS, _cfg("good")) is not None
    assert api == ["broke", "good"]


@pytest.mark.asyncio
async def test_transient_errors_do_not_trip_the_breaker(api):
    for _ in range(3):
        assert await ts.system_one("s", QUESTIONS, _cfg("flaky")) is None
    assert len(api) == 3          # 5xx 可能下次就好,照常每次都试


@pytest.mark.asyncio
async def test_cooldown_expires_then_retries_with_longer_pause(api, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ts, "_now", lambda: now[0])
    await ts.system_one("s", QUESTIONS, _cfg("broke"))
    now[0] += 61                  # 60 秒冷却过去
    await ts.system_one("s", QUESTIONS, _cfg("broke"))
    assert len(api) == 2
    now[0] += 61                  # 第二次失败后冷却翻倍到 120 秒,61 秒还没到
    await ts.system_one("s", QUESTIONS, _cfg("broke"))
    assert len(api) == 2
