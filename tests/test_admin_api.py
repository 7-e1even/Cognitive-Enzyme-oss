"""后台管理接口(/api/admin/*)的契约测试:内存版存储 + 进程内 FastAPI 客户端。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin_fakes import FakeAdminPG
from app import model_registry, settings
from app.config import Config
from app.routers import admin
from app.routers.auth import get_current_user, require_admin
from app.security import hash_password

ADMIN = {"id": "u-admin", "username": "root", "role": "admin", "is_active": True}


class _Qdrant:
    def update_embedding_config(self, cfg):
        self.cfg = cfg


class _Engine:
    def __init__(self, pg):
        self.pg = pg
        self.qdrant = _Qdrant()
        self.config = Config()


@pytest.fixture
def client():
    saved_values = dict(settings._values)
    saved_registry = model_registry.current()
    settings._values.clear()
    pg = FakeAdminPG()
    pg.users[ADMIN["id"]] = {**ADMIN, "password_hash": hash_password("x" * 12), "display_name": "root",
                             "avatar_url": "", "created_at": None, "updated_at": None}
    app = FastAPI()
    app.include_router(admin.router)
    app.state.engine = _Engine(pg)
    app.state.config = app.state.boot_config = Config()
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as c:
        c.pg = pg
        yield c
    settings._values.clear()
    settings._values.update(saved_values)
    model_registry.set_current(saved_registry)


def test_settings_roundtrip(client):
    snap = client.get("/api/admin/settings").json()
    assert snap["groups"][0]["key"] == "model" and len(snap["settings"]) == len(settings.SPECS)

    r = client.patch("/api/admin/settings", json={"values": {"judge.pool": 24, "recall.lexical": False}})
    assert r.status_code == 200 and set(r.json()["changed"]) == {"judge.pool", "recall.lexical"}
    assert settings.get("judge.pool") == 24

    r = client.patch("/api/admin/settings", json={"values": {"judge.pool": 0}})
    assert r.status_code == 422 and r.json()["detail"]["key"] == "judge.pool"

    r = client.patch("/api/admin/settings", json={"reset": ["recall.lexical"]})
    item = {s["key"]: s for s in r.json()["settings"]}["recall.lexical"]
    assert item["source"] == "default" and item["value"] is True

    r = client.patch("/api/admin/settings", json={"values": {"model.llm": "missing"}})
    assert r.status_code == 422
    assert [a["action"] for a in client.pg.audit_rows] == ["settings.update", "settings.update"]


def _provider(client, pid="sf", protocol="openai", url="https://api.siliconflow.cn/v1/", key="sk-test-123456789"):
    return client.post("/api/admin/providers", json={"id": pid, "name": pid, "protocol": protocol,
                                                     "base_url": url, "api_key": key})


def test_providers_and_models(client):
    r = _provider(client)
    assert r.status_code == 200
    body = r.json()
    assert body["base_url"] == "https://api.siliconflow.cn/v1" and body["api_key_hint"] == "••••6789"
    assert "api_key" not in body                                         # 密钥只写不读
    assert _provider(client).status_code == 409
    assert _provider(client, "bad", url="ftp://x").status_code == 422
    assert _provider(client, "ts", "system_one", "https://api.typesafe.ai/v1", "ts").status_code == 200

    r = client.post("/api/admin/models", json={"id": "bge-m3", "provider_id": "sf", "kind": "embedding",
                                               "model": "BAAI/bge-m3", "params": {"dimensions": 1024}})
    assert r.status_code == 200 and r.json()["params"] == {"dimensions": 1024}
    r = client.post("/api/admin/models", json={"id": "x", "provider_id": "ts", "kind": "chat", "model": "m"})
    assert r.status_code == 422                                          # 协议不匹配
    r = client.post("/api/admin/models", json={"id": "jev", "provider_id": "ts", "kind": "judge", "model": "j"})
    assert r.status_code == 422                                          # 保留名
    assert client.post("/api/admin/models", json={"id": "jev-1.13.0", "provider_id": "ts", "kind": "judge",
                                                  "model": "jev-1.13.0"}).status_code == 200

    # 改密钥:不传保留、传新值覆盖、clear 清空
    r = client.patch("/api/admin/providers/sf", json={"name": "SiliconFlow"})
    assert r.json()["api_key_set"] is True
    r = client.patch("/api/admin/providers/sf", json={"clear_api_key": True})
    assert r.json()["api_key_set"] is False

    # 服务商下还有模型 → 409;强制删除连模型一起删
    assert client.delete("/api/admin/providers/sf").status_code == 409
    assert client.delete("/api/admin/providers/sf", params={"force": True}).json()["models"] == ["bge-m3"]
    assert "bge-m3" not in client.pg.models


def test_role_binding_and_in_use_guard(client):
    _provider(client)
    client.post("/api/admin/models", json={"id": "bge-m3", "provider_id": "sf", "kind": "embedding",
                                           "model": "BAAI/bge-m3", "params": {"dimensions": 1024}})
    r = client.put("/api/admin/models/roles", json={"bindings": {"model.embedding": "bge-m3"}})
    roles = {x["key"]: x for x in r.json()["roles"]}
    assert roles["model.embedding"]["source"] == "db" and roles["model.embedding"]["model"] == "BAAI/bge-m3"
    assert "model.embedding" in r.json()["restart_required"]            # 维度 1536 → 1024
    assert client.app.state.config.embedding.dimensions == 1024          # 已热更新

    r = client.put("/api/admin/models/roles", json={"bindings": {"model.rerank": "bge-m3"}})
    assert r.status_code == 422                                          # 类型不符
    assert client.delete("/api/admin/models/bge-m3").status_code == 409
    r = client.delete("/api/admin/models/bge-m3", params={"force": True})
    assert r.json()["unbound"] == ["model.embedding"] and settings.source("model.embedding") == "default"


def test_users_guards(client):
    r = client.post("/api/admin/users", json={"username": "ops", "password": "ops-pass-123", "role": "user"})
    assert r.status_code == 200 and r.json()["role"] == "user"
    assert client.post("/api/admin/users", json={"username": "ops", "password": "ops-pass-123"}).status_code == 409
    assert client.post("/api/admin/users", json={"username": "a", "password": "short"}).status_code == 422
    assert client.patch(f"/api/admin/users/{ADMIN['id']}", json={"role": "user"}).status_code == 422
    assert client.patch(f"/api/admin/users/{ADMIN['id']}", json={"is_active": False}).status_code == 422
    assert client.delete(f"/api/admin/users/{ADMIN['id']}").status_code == 422
    uid = r.json()["id"]
    r = client.patch(f"/api/admin/users/{uid}", json={"password": "new-pass-456", "is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    detail = [a for a in client.pg.audit_rows if a["action"] == "user.update"][0]["detail"]
    assert detail.get("password") == "reset" and "password_hash" not in detail


def test_security_tokens(client, monkeypatch):
    from app import mcp_server
    monkeypatch.setattr(mcp_server.mcp, "auth", None)
    r = client.post("/api/admin/security/tokens/api/rotate")
    token = r.json()["api_token"]
    assert r.status_code == 200 and len(token) >= 32 and r.json()["restart_required"] is False
    assert client.put("/api/admin/security/tokens/api", json={"token": "short"}).status_code == 422
    r = client.post("/api/admin/security/tokens/mcp/rotate")
    assert r.json()["restart_required"] is True                          # MCP 认证原本没开:需要重启
    r = client.delete("/api/admin/security/tokens/api")
    assert r.json()["api_token"] == ""
    assert client.post("/api/admin/security/tokens/other/rotate").status_code == 404
    actions = [a["action"] for a in client.pg.audit_rows]
    assert actions.count("security.api_token") == 2 and "security.mcp_token" in actions
    assert all(token not in str(a["detail"]) for a in client.pg.audit_rows)   # 审计不记令牌本身
