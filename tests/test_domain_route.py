"""召回时的领域分流:判官选领域 → 专家通道补召回 / 严格模式只留选中领域;判官出问题时照常全库召回。"""

import asyncio

import pytest

from app.config import TypeSafeConfig
from app.core import Enzyme, Pattern
from app.matching import pipeline as pl
from app.matching import route

TS = TypeSafeConfig(api_key="k", api_url="http://ts")


def _e(eid, domain):
    return Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}", domain=domain)


class _PG:
    def __init__(self, *es):
        self.e = {e.id: e for e in es}

    async def get_enzymes(self, ids):
        return [self.e[i] for i in ids if i in self.e]

    async def routable_domains(self):
        return [{"name": "legal", "description": "判例"}, {"name": "security", "description": "漏洞"},
                {"name": "biomed", "description": "论文"}]


class _Q:
    """全库检索只找得到 g1(legal)、g2(security);按领域检索各有自己的记忆。"""

    def __init__(self):
        self.domain_calls = []

    async def search(self, vector, domain=None, limit=50):
        if domain is None:
            return [{"id": "g1", "_score": 0.9}, {"id": "g2", "_score": 0.8}]
        self.domain_calls.append((domain, limit))
        return {"legal": [{"id": "g1", "_score": 0.9}, {"id": "l2", "_score": 0.6}, {"id": "l3", "_score": 0.7}],
                "security": [{"id": "s2", "_score": 0.5}]}.get(domain, [])

    async def search_lexical(self, text, domain=None, limit=10):
        return []

    async def search_exemplars(self, vector, domain=None, limit=30):
        return []


LIBRARY = [_e("g1", "legal"), _e("g2", "security"), _e("l2", "legal"), _e("l3", "legal"), _e("s2", "security")]


@pytest.fixture
def judge(monkeypatch):
    state = {"probs": None, "delay": 0.0, "calls": 0}

    async def fake_system_one(s, questions, config, timeout=10.0):
        state["calls"] += 1
        await asyncio.sleep(state["delay"])
        return None if state["probs"] is None else {"answers": {"route": {"probabilities": state["probs"]}}}

    async def fake_embed(text, config=None):
        return [0.1]

    monkeypatch.setattr(route, "system_one", fake_system_one)
    monkeypatch.setattr(route, "_cache", None)
    monkeypatch.setattr("app.llm_client.embed", fake_embed)
    return state


@pytest.mark.asyncio
async def test_pick_domains_threshold_order_and_none(judge, monkeypatch):
    judge["probs"] = {"d0": 0.55, "d1": 0.35, "d2": 0.02, "none": 0.08}
    got = await route.pick_domains("合同违约怎么判", _PG(), TS)
    assert got["domains"] == [("legal", 0.55), ("security", 0.35)] and got["none"] == 0.08
    judge["probs"] = {"d0": 0.2, "d1": 0.1, "d2": 0.0, "none": 0.7}           # 通用问题:不分流
    assert (await route.pick_domains("写个排序", _PG(), TS))["domains"] == []
    monkeypatch.setenv("CE_DOMAIN_ROUTE_K", "1")
    judge["probs"] = {"d0": 0.55, "d1": 0.35, "d2": 0.02, "none": 0.08}
    assert [d for d, _ in (await route.pick_domains("q", _PG(), TS))["domains"]] == ["legal"]
    assert await route.pick_domains("q", _PG(), None) is None                   # 没配判官


@pytest.mark.asyncio
async def test_switched_off_never_asks_the_judge(judge, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_ROUTE", "off")
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), None, None, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "g2"] and judge["calls"] == 0


@pytest.mark.asyncio
async def test_expert_is_the_default(judge):
    judge["probs"] = {"d0": 0.8, "d1": 0.1, "d2": 0.0, "none": 0.1}
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), None, None, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "l3", "l2", "g2"] and judge["calls"] == 1


@pytest.mark.asyncio
async def test_expert_mode_adds_in_domain_candidates_after_global_ones(judge, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_ROUTE", "expert")
    judge["probs"] = {"d0": 0.8, "d1": 0.1, "d2": 0.0, "none": 0.1}
    q, trace = _Q(), {}
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), q, None, None, trace=trace, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "l3", "l2", "g2"]      # 选中领域的排前面(补进来的按分数),其余留在后面
    assert q.domain_calls == [("legal", 20)] and trace["route"]["added_ids"] == ["l3", "l2"]


@pytest.mark.asyncio
async def test_strict_mode_keeps_only_chosen_domains(judge, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_ROUTE", "strict")
    judge["probs"] = {"d0": 0.8, "d1": 0.1, "d2": 0.0, "none": 0.1}
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), None, None, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "l3", "l2"]


@pytest.mark.asyncio
async def test_slow_or_failed_judge_falls_back_to_all_domains(judge, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_ROUTE", "strict")
    monkeypatch.setenv("CE_DOMAIN_ROUTE_TIMEOUT", "0.2")
    judge["probs"], judge["delay"] = {"d0": 0.9, "none": 0.1}, 1.0
    trace = {}
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), None, None, trace=trace, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "g2"] and trace["route"]["status"] == "skipped"
    judge["probs"], judge["delay"] = None, 0.0                                 # 判官出错
    got = await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), None, None, typesafe_config=TS)
    assert [e.id for e in got] == ["g1", "g2"]


@pytest.mark.asyncio
async def test_explicit_domain_filter_skips_routing(judge, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_ROUTE", "expert")
    judge["probs"] = {"d0": 0.8, "none": 0.2}
    await pl._recall("q", "ctx", _PG(*LIBRARY), _Q(), "security", None, typesafe_config=TS)
    assert judge["calls"] == 0
