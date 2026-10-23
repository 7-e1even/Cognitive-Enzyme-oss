"""首页接口(/api/home)的契约测试:内存版存储 + 进程内 FastAPI 客户端。"""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import settings
from app.config import Config, EmbeddingConfig, LLMConfig
from app.routers import graph

SUMMARY = {
    "impact": {"days": 7, "primes": 12, "injected": 20, "helped": 6, "harmed": 1},
    "series": [{"day": "2026-09-25", "injected": 20, "helped": 6}],
    "last_prime_at": "2026-09-25T10:00:00+00:00",
    "last_consolidation_at": None,
    "pending_review": 3,
    "failed_jobs": 0,
}


class _PG:
    def __init__(self):
        self.calls = []

    async def home_summary(self, days, series_days, tz):
        self.calls.append((days, series_days, tz))
        return SUMMARY


class _Qdrant:
    def __init__(self, ok=True):
        self.ok = ok

    async def ping(self):
        if isinstance(self.ok, Exception):
            raise self.ok
        return self.ok


class _Engine:
    def __init__(self, qdrant):
        self.pg = _PG()
        self.qdrant = qdrant


def _client(config: Config, qdrant) -> TestClient:
    app = FastAPI()
    app.include_router(graph.router)
    app.state.engine = _Engine(qdrant)
    app.state.config = config
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_settings():
    saved = dict(settings._values)
    settings._values.clear()
    yield
    settings._values.clear()
    settings._values.update(saved)


def test_home_reports_setup_gaps_and_summary():
    body = _client(Config(), _Qdrant(True)).get("/api/home").json()

    assert body["storage"] == {"postgresql": True, "qdrant": True}
    assert body["capabilities"] == {"llm": False, "embedding": False, "rerank": False, "judge": False}
    assert body["decision_log"] is True  # 判定记录默认开
    assert body["impact"] == SUMMARY["impact"] and body["pending_review"] == 3
    assert body["last_prime_at"] == SUMMARY["last_prime_at"]


def test_home_asks_store_for_one_week_and_two_week_series_in_viewer_timezone():
    client = _client(Config(), _Qdrant(True))
    client.get("/api/home", params={"tz": "America/New_York"})
    assert client.app.state.engine.pg.calls == [(7, 14, "America/New_York")]


@pytest.mark.parametrize("tz", ["Not/AZone", "../../etc/passwd", ""])
def test_home_falls_back_to_utc_for_unknown_timezones(tz):
    client = _client(Config(), _Qdrant(True))
    assert client.get("/api/home", params={"tz": tz}).status_code == 200
    assert client.app.state.engine.pg.calls[-1][2] == "UTC"


def test_home_marks_qdrant_down_instead_of_failing():
    body = _client(Config(), _Qdrant(ConnectionError("refused"))).get("/api/home").json()
    assert body["storage"]["qdrant"] is False


def test_home_capabilities_follow_bound_models():
    config = replace(
        Config(),
        llm=LLMConfig(api_key="k", api_url="https://llm.example/v1"),
        embedding=EmbeddingConfig(api_key="k", api_url="https://emb.example/v1"),
    )
    caps = _client(config, _Qdrant(True)).get("/api/home").json()["capabilities"]
    assert caps["llm"] and caps["embedding"] and not caps["rerank"] and not caps["judge"]


def test_flow_reports_effective_settings_and_schedule():
    """流程页按实际配置写:后台改了的值原样反映出来,不含密钥类字段。"""
    client = _client(Config(), _Qdrant())
    with settings.override({"judge.pool": 24, "learning.dedup": False}):
        data = client.get("/api/flow").json()
    assert data["settings"]["judge.pool"] == 24 and data["settings"]["learning.dedup"] is False
    assert data["settings"]["domain.auto"] is True and data["settings"]["selection.none_max"] == 0.5
    assert data["quality"] == {"pass": 3.5, "refine": 2.5, "quarantine": 2.0}
    assert data["schedule"]["interval"] > 0 and data["schedule"]["due_in"] >= 0
    assert not any("key" in k or "token" in k for k in data["settings"])
