"""运行参数注册表:取值优先级、校验、保存 / 重置,以及与改造前环境变量语义的兼容。"""

import json

import pytest

from admin_fakes import FakeAdminPG
from app import settings


@pytest.fixture(autouse=True)
def _clean_cache():
    saved = dict(settings._values)
    settings._values.clear()
    yield
    settings._values.clear()
    settings._values.update(saved)


def test_every_spec_is_well_formed():
    groups = {g.key for g in settings.GROUPS}
    envs = [s.env for s in settings.SPECS_LIST if s.env]
    assert len(envs) == len(set(envs)), "同一个环境变量不能对应两个参数"
    for spec in settings.SPECS_LIST:
        assert spec.group in groups, spec.key
        assert spec.type in settings.TYPES, spec.key
        assert settings.coerce(spec, spec.default) == spec.default, spec.key
        if spec.type == "enum":
            assert spec.default in {c for c, _ in spec.choices}, spec.key


def test_precedence_db_over_env_over_default(monkeypatch):
    monkeypatch.delenv("CE_JEV_POOL", raising=False)
    assert settings.get("judge.pool") == 36 and settings.source("judge.pool") == "default"
    monkeypatch.setenv("CE_JEV_POOL", "24")
    assert settings.get("judge.pool") == 24 and settings.source("judge.pool") == "env"
    with settings.override({"judge.pool": 50}):
        assert settings.get("judge.pool") == 50 and settings.source("judge.pool") == "db"
    assert settings.get("judge.pool") == 24


def test_env_semantics_match_legacy(monkeypatch):
    monkeypatch.setenv("CE_LEXICAL_RECALL", "off")
    assert settings.get("recall.lexical") is False
    monkeypatch.setenv("CE_JEV_MODE", "garbage")          # 非法值回退默认(旧代码同样回退 on)
    assert settings.get("judge.enabled") is True
    monkeypatch.setenv("CE_EXEMPLAR_ANALOGY", "SHADOW")
    assert settings.get("exemplar.analogy") == "shadow"
    monkeypatch.setenv("CE_SHADOW_JUDGES", "laya-local, glm-5.3,")
    assert settings.get("distill.shadow_judges") == ["laya-local", "glm-5.3"]
    # 旧代码里有 max(1, …) 的参数环境变量也截断;其余参数环境变量不受后台 min 约束(评测旋钮原样生效)
    monkeypatch.setenv("CE_JEV_POOL", "0")
    assert settings.get("judge.pool") == 1
    monkeypatch.setenv("CE_JEV_TIMEOUT", "0.3")
    assert settings.get("judge.timeout") == 0.3
    monkeypatch.setenv("CE_RELEVANCE_FLOOR", "")           # 空值视为未设置
    assert settings.get("ranking.relevance_floor") == 0.01


def test_coerce_validates_admin_input():
    spec = settings.SPECS["judge.pool"]
    assert settings.coerce(spec, "12") == 12
    with pytest.raises(settings.SettingError):
        settings.coerce(spec, 0)
    with pytest.raises(settings.SettingError):
        settings.coerce(spec, 1.5)
    with pytest.raises(settings.SettingError):
        settings.coerce(settings.SPECS["judge.stage"], "sideways")
    assert settings.coerce(settings.SPECS["gateway.route"], "a, b") == ["a", "b"]
    assert settings.coerce(settings.SPECS["recall.lexical"], "off") is False


@pytest.mark.asyncio
async def test_save_reset_and_reload():
    pg = FakeAdminPG()
    changes = await settings.save(pg, {"judge.pool": 24, "exemplar.analogy": "off"})
    assert {c["key"] for c in changes} == {"judge.pool", "exemplar.analogy"}
    assert json.loads(pg.settings["cfg:judge.pool"][0]) == 24
    assert settings.get("judge.pool") == 24

    # 同值再存不算变更
    assert await settings.save(pg, {"judge.pool": 24}) == []

    # 另一个进程看到的是数据库:load 之后一致
    settings._values.clear()
    await settings.load(pg)
    assert settings.get("exemplar.analogy") == "off"

    changes = await settings.save(pg, reset=["exemplar.analogy"])
    assert changes[0]["after"] == "on" and "cfg:exemplar.analogy" not in pg.settings


@pytest.mark.asyncio
async def test_invalid_batch_writes_nothing():
    pg = FakeAdminPG()
    with pytest.raises(settings.SettingError):
        await settings.save(pg, {"judge.pool": 24, "judge.timeout": -1})
    assert pg.settings == {}
    with pytest.raises(settings.SettingError):
        await settings.save(pg, {"no.such": 1})


@pytest.mark.asyncio
async def test_read_does_not_touch_cache():
    pg = FakeAdminPG({"cfg:judge.pool": "12", "cfg:judge.stage": '"bogus"'})
    values = await settings.read(pg)
    assert values == {"judge.pool": 12}               # 非法存储值被忽略
    assert "judge.pool" not in settings._values       # 评测脚本读生产库时不会污染旋钮


def test_snapshot_shape():
    snap = settings.snapshot()
    assert [g["key"] for g in snap["groups"]][0] == "model"
    item = next(s for s in snap["settings"] if s["key"] == "judge.stage")
    assert item["choices"] and item["source"] in ("db", "env", "default") and item["env"] == "CE_JEV_STAGE"


def test_selection_thresholds_come_from_registry(monkeypatch):
    from app.matching.selection import THRESHOLDS, thresholds
    assert THRESHOLDS["choice_min"] == ("CE_JEV_CHOICE_MIN", 0.5)
    monkeypatch.setenv("CE_JEV_SUPPLEMENT_P", "0.9")
    with settings.override({"selection.rescue_q": 0.8}):
        t = thresholds()
    assert t["supplement_p"] == 0.9 and t["rescue_q"] == 0.8 and set(t) == set(THRESHOLDS)


def test_methodology_hint_lane_is_enabled_by_default():
    assert settings.SPECS["selection.hint_max"].default == 2
