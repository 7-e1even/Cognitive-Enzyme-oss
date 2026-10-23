"""模型注册表:角色绑定解析、校验、旧配置迁移 / 导入、老师注册表。"""

import json

import pytest

from admin_fakes import FakeAdminPG
from app import model_registry as mr
from app import settings
from app.config import Config, EmbeddingConfig, LLMConfig, TypeSafeConfig, load_runtime_config


def _reg():
    providers = {
        "sf": mr.Provider("sf", "SiliconFlow", "openai", "https://api.siliconflow.cn/v1", "sk-sf"),
        "claude": mr.Provider("claude", "Anthropic", "anthropic", "https://api.anthropic.com", "sk-ant"),
        "ts": mr.Provider("ts", "TypeSafe", "system_one", "https://api.typesafe.ai/v1", "ts-key"),
        "off": mr.Provider("off", "Off", "openai", "https://off/v1", "k", enabled=False),
    }
    models = {
        "bge-m3": mr.Model("bge-m3", "sf", "embedding", "BAAI/bge-m3", {"dimensions": 1024}),
        "v2-m3": mr.Model("v2-m3", "sf", "rerank", "BAAI/bge-reranker-v2-m3", {"top_n": 20}),
        "sonnet": mr.Model("sonnet", "claude", "chat", "claude-sonnet-5", {"temperature": None, "max_tokens": 4000}),
        "jev-1.13.0": mr.Model("jev-1.13.0", "ts", "judge", "jev-1.13.0"),
        "dead": mr.Model("dead", "off", "chat", "x"),
    }
    return mr.Registry(providers, models)


def test_apply_roles_overrides_only_bound_roles():
    base = Config(llm=LLMConfig(api_key="env", api_url="https://env/v1", model="env-model"),
                  embedding=EmbeddingConfig(api_key="e", api_url="https://e/v1", rerank_api_url="https://e/v1/rerank"),
                  typesafe=TypeSafeConfig(api_key="env-ts"))
    values = {"model.embedding": "bge-m3", "model.rerank": "v2-m3", "model.judge": "jev-1.13.0"}
    cfg = mr.apply_roles(base, _reg(), values)
    assert cfg.llm.model == "env-model"                                  # 未绑定:沿用环境变量
    assert (cfg.embedding.model, cfg.embedding.dimensions, cfg.embedding.api_key) == ("BAAI/bge-m3", 1024, "sk-sf")
    assert cfg.embedding.rerank_api_url == "https://api.siliconflow.cn/v1/rerank"
    assert cfg.embedding.rerank_top_n == 20 and cfg.embedding.rerank_api_key == "sk-sf"
    assert (cfg.typesafe.api_key, cfg.typesafe.model) == ("ts-key", "jev-1.13.0")


def test_anthropic_chat_role_and_disabled_provider():
    cfg = mr.apply_roles(Config(), _reg(), {"model.llm": "sonnet", "model.query_expand": "dead"})
    assert cfg.llm.protocol == "anthropic" and cfg.llm.temperature is None and cfg.llm.max_tokens == 4000
    assert cfg.expand_llm is None                                        # 停用的服务商 → 不生效


def test_role_status_reports_sources():
    reg = _reg()
    config = mr.apply_roles(Config(llm=LLMConfig(api_key="k", api_url="https://env/v1")), reg,
                            {"model.embedding": "bge-m3"})
    status = {r["key"]: r for r in mr.role_status(config, reg, {"model.embedding": "bge-m3", "model.rerank": "nope"})}
    assert status["model.embedding"]["source"] == "db"
    assert status["model.llm"]["source"] == "env"
    assert status["model.rerank"]["source"] == "none" and "error" in status["model.rerank"]
    assert status["model.query_expand"]["source"] == "inherit"


def test_validation():
    with pytest.raises(mr.RegistryError):
        mr.check_id("bad id!", "模型")
    with pytest.raises(mr.RegistryError):
        mr.check_provider({"protocol": "grpc", "base_url": "https://x"})
    with pytest.raises(mr.RegistryError):
        mr.check_provider({"protocol": "openai", "base_url": "ftp://x"})
    providers = {"ts": {"protocol": "system_one"}, "sf": {"protocol": "openai"}}
    with pytest.raises(mr.RegistryError):
        mr.check_model({"kind": "chat", "provider_id": "ts", "model": "x"}, providers)   # 协议不匹配
    ok = mr.check_model({"kind": "chat", "provider_id": "sf", "model": " glm ",
                         "params": {"temperature": None, "max_tokens": "256", "bogus": 1}}, providers)
    assert ok["model"] == "glm" and ok["params"] == {"temperature": None, "max_tokens": 256}


@pytest.mark.asyncio
async def test_migrate_legacy_runtime_keys(monkeypatch):
    for var in ("CE_LLM_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    pg = FakeAdminPG({
        "runtime.llm.api_key": "sk-llm", "runtime.llm.api_url": "https://open.bigmodel.cn/api/paas/v4",
        "runtime.llm.model": "glm-5.3",
        "runtime.embedding.api_key": "sk-sf", "runtime.embedding.api_url": "https://api.siliconflow.cn/v1",
        "runtime.embedding.model": "BAAI/bge-m3", "runtime.embedding.dimensions": "1024",
        "runtime.rerank.api_url": "https://api.siliconflow.cn/v1/rerank", "runtime.rerank.model": "BAAI/bge-reranker-v2-m3",
        "runtime.qdrant.collection": "enzymes_prod",
    })
    before = await load_runtime_config(pg, Config.from_env())
    done = await mr.migrate_legacy(pg)
    assert done and not any(k.startswith("runtime.") for k in pg.settings)
    assert set(pg.providers) == {"bigmodel", "siliconflow"}              # 向量与精排共用一个服务商
    after = await load_runtime_config(pg, Config.from_env())
    # 迁移前后生效配置一致
    assert (after.llm.api_url, after.llm.model, after.llm.api_key) == (before.llm.api_url, before.llm.model, "sk-llm")
    assert (after.embedding.model, after.embedding.dimensions) == ("BAAI/bge-m3", 1024)
    assert after.embedding.rerank_api_url == before.embedding.rerank_api_url
    assert after.qdrant.collection_name == "enzymes_prod"
    assert await mr.migrate_legacy(pg) == []                              # 幂等


@pytest.mark.asyncio
async def test_import_from_env_binds_unbound_roles(monkeypatch, tmp_path):
    f = tmp_path / "teachers.json"
    f.write_text(json.dumps([{"name": "glm-5.3", "kind": "openai", "url": "https://open.bigmodel.cn/api/paas/v4",
                              "model": "glm-5.3", "key_env": "GLM_API_KEY"}]))
    monkeypatch.setenv("CE_TEACHERS_FILE", str(f))
    monkeypatch.setenv("GLM_API_KEY", "sk-glm")
    monkeypatch.delenv("CE_TEACHERS", raising=False)
    pg = FakeAdminPG()
    config = Config(typesafe=TypeSafeConfig(api_key="ts-key"),
                    llm=LLMConfig(api_key="sk-glm", api_url="https://open.bigmodel.cn/api/paas/v4", model="glm-5.3"))
    result = await mr.import_from_env(pg, config)
    assert result["bindings"]["model.judge"] == "jev-1.13.0"
    assert result["bindings"]["model.llm"] == "glm-5.3"
    assert result["teachers"] == ["glm-5.3"]                               # 同一连接同一模型复用,不重复建
    assert len(pg.providers) == 2 and pg.providers["bigmodel"]["api_key"] == "sk-glm"


@pytest.mark.asyncio
async def test_teachers_come_from_registry(monkeypatch):
    from app.distill import teachers as T
    monkeypatch.delenv("CE_TEACHERS_FILE", raising=False)
    monkeypatch.delenv("CE_TEACHERS", raising=False)
    saved = mr.current()
    try:
        mr.set_current(_reg())
        with settings.override({"model.judge": "jev-1.13.0"}):
            specs = T.load_specs()
        assert specs["jev"].api_key == "ts-key" and specs["jev"].url == "https://api.typesafe.ai/v1"
        assert specs["sonnet"].kind == "anthropic" and specs["sonnet"].temperature is None
        assert "dead" not in specs and "bge-m3" not in specs            # 停用的 / 非对话非判官的不当老师
    finally:
        mr.set_current(saved)


def test_registry_change_clears_teacher_caches():
    from app.distill import gateway as G
    from app.distill import teachers as T
    T._CACHE["x"] = object()
    G._TEACHERS["x"] = object()
    saved = mr.current()
    mr.set_current(mr.Registry())
    mr.set_current(saved)
    assert "x" not in T._CACHE and "x" not in G._TEACHERS
