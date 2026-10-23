"""后台管理接口(仅管理员):运行参数、模型注册表、用户、审计日志、后台任务、系统状态、
判定记录与召回调试台。

写操作都记审计日志(密钥只记是否修改,不记内容);运行参数与模型的改动保存后立即热更新引擎配置
(app/runtime.py),只有向量库连接与向量维度需要重启。
"""

from __future__ import annotations

import logging
import platform
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import model_registry, runtime, settings
from app.dependencies import get_engine
from app.model_registry import RegistryError
from app.routers._helpers import audit, capabilities
from app.routers.auth import require_admin
from app.security import auth_enabled, hash_password, invalidate_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

_STARTED_AT = datetime.now(timezone.utc)
_ROLES = ("admin", "user")


def _hint(secret: str) -> str:
    return f"••••{secret[-4:]}" if len(secret) >= 12 else ("••••" if secret else "")


def _bad(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


# ── 运行参数 ──────────────────────────────────────────────────────

class SettingsUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    reset: list[str] = Field(default_factory=list)


def _check_model_refs(values: dict[str, Any]) -> None:
    reg = model_registry.current()
    for key, value in values.items():
        spec = settings.SPECS.get(key)
        if spec is None or spec.type not in ("model", "models"):
            continue
        ids = [value] if spec.type == "model" else (value if isinstance(value, list) else
                                                    [v.strip() for v in str(value).split(",")])
        for mid in ids:
            if not mid or (spec.type == "models" and mid == model_registry.JUDGE_ALIAS):
                continue
            m = reg.models.get(mid)
            if m is None:
                raise _bad(f"{spec.label}:模型 {mid} 不存在")
            if spec.kinds and m.kind not in spec.kinds:
                raise _bad(f"{spec.label}:只能选择 {' / '.join(spec.kinds)} 类型的模型,{mid} 是 {m.kind}")


@router.get("/settings")
async def get_settings(request: Request):
    return {**settings.snapshot(), "restart_required": runtime.restart_required(request.app)}


@router.patch("/settings")
async def update_settings(
    body: SettingsUpdate,
    request: Request,
    user: dict = Depends(require_admin),
    engine=Depends(get_engine),
):
    _check_model_refs(body.values)
    try:
        changes = await settings.save(engine.pg, body.values, body.reset)
    except settings.SettingError as e:
        raise HTTPException(status_code=422, detail={"key": e.key, "message": e.message})
    if changes:
        await audit(engine, user, "settings.update", ", ".join(c["key"] for c in changes), {"changes": changes})
        await runtime.reload(request.app)
    return {**settings.snapshot(), "changed": [c["key"] for c in changes],
            "restart_required": runtime.restart_required(request.app)}


# ── 模型注册表:服务商 ────────────────────────────────────────────

class ProviderIn(BaseModel):
    id: str
    name: str = ""
    protocol: str
    base_url: str
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    note: str = ""


class ProviderPatch(BaseModel):
    name: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None        # None = 不修改
    clear_api_key: bool = False
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    note: str | None = None


def _provider_out(row: dict, models: list[dict]) -> dict:
    mine = [m for m in models if m["provider_id"] == row["id"]]
    return {
        "id": row["id"], "name": row.get("name") or row["id"], "protocol": row["protocol"],
        "base_url": row["base_url"], "api_key_set": bool(row.get("api_key")),
        "api_key_hint": _hint(row.get("api_key") or ""), "headers": row.get("headers") or {},
        "enabled": row["enabled"], "note": row.get("note") or "",
        "models": [m["id"] for m in mine],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@router.get("/providers")
async def list_providers(engine=Depends(get_engine)):
    models = await engine.pg.list_models()
    return {"providers": [_provider_out(p, models) for p in await engine.pg.list_providers()]}


@router.post("/providers")
async def create_provider(body: ProviderIn, request: Request, user: dict = Depends(require_admin),
                          engine=Depends(get_engine)):
    try:
        pid = model_registry.check_id(body.id, "服务商")
        data = model_registry.check_provider({**body.model_dump(), "id": pid, "name": body.name.strip() or pid,
                                              "api_key": body.api_key.strip()})
    except RegistryError as e:
        raise _bad(str(e))
    if await engine.pg.get_provider(pid):
        raise _bad(f"服务商 {pid} 已存在", 409)
    row = await engine.pg.create_provider(data)
    await audit(engine, user, "provider.create", pid, {
        "protocol": data["protocol"], "base_url": data["base_url"], "api_key": bool(data["api_key"])})
    await runtime.reload(request.app)
    return _provider_out(row, [])


@router.patch("/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderPatch, request: Request,
                          user: dict = Depends(require_admin), engine=Depends(get_engine)):
    current = await engine.pg.get_provider(provider_id)
    if current is None:
        raise _bad("服务商不存在", 404)
    fields = body.model_dump(exclude_unset=True, exclude={"api_key", "clear_api_key"})
    if body.clear_api_key:
        fields["api_key"] = ""
    elif body.api_key is not None and body.api_key.strip():
        fields["api_key"] = body.api_key.strip()
    merged = {**current, **fields}
    try:
        checked = model_registry.check_provider(merged)
    except RegistryError as e:
        raise _bad(str(e))
    if "base_url" in fields:
        fields["base_url"] = checked["base_url"]
    if merged["protocol"] != current["protocol"]:
        models = [m for m in await engine.pg.list_models() if m["provider_id"] == provider_id]
        bad = [m["id"] for m in models if merged["protocol"] not in model_registry.KIND_PROTOCOLS[m["kind"]]]
        if bad:
            raise _bad(f"下列模型不支持 {merged['protocol']} 协议:{', '.join(bad)}")
    row = await engine.pg.update_provider(provider_id, fields)
    detail = {k: v for k, v in fields.items() if k != "api_key"}
    if "api_key" in fields:
        detail["api_key"] = "cleared" if not fields["api_key"] else "changed"
    await audit(engine, user, "provider.update", provider_id, detail)
    await runtime.reload(request.app)
    return _provider_out(row, await engine.pg.list_models())


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str, request: Request, force: bool = False,
                          user: dict = Depends(require_admin), engine=Depends(get_engine)):
    if await engine.pg.get_provider(provider_id) is None:
        raise _bad("服务商不存在", 404)
    models = [m["id"] for m in await engine.pg.list_models() if m["provider_id"] == provider_id]
    if models and not force:
        raise _bad(f"该服务商下还有模型:{', '.join(models)}。先删除这些模型,或强制删除(一并删除)。", 409)
    for mid in models:
        await _unbind_model(engine, mid)
    await engine.pg.delete_provider(provider_id, cascade=True)
    await audit(engine, user, "provider.delete", provider_id, {"models": models})
    await runtime.reload(request.app)
    return {"status": "deleted", "id": provider_id, "models": models}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str, engine=Depends(get_engine)):
    row = await engine.pg.get_provider(provider_id)
    if row is None:
        raise _bad("服务商不存在", 404)
    return await model_registry.probe_provider(model_registry.Provider.from_row(row))


# ── 模型注册表:模型与角色绑定 ────────────────────────────────────

class ModelIn(BaseModel):
    id: str
    provider_id: str
    kind: str
    model: str
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    note: str = ""


class ModelPatch(BaseModel):
    provider_id: str | None = None
    kind: str | None = None
    model: str | None = None
    params: dict[str, Any] | None = None
    enabled: bool | None = None
    note: str | None = None


class ModelTest(BaseModel):
    prompt: str = ""


class RoleBindings(BaseModel):
    bindings: dict[str, str]


def _model_out(row: dict, providers: dict[str, dict], values: dict) -> dict:
    p = providers.get(row["provider_id"]) or {}
    return {
        "id": row["id"], "provider_id": row["provider_id"], "provider_name": p.get("name") or row["provider_id"],
        "protocol": p.get("protocol", ""), "kind": row["kind"], "model": row["model"],
        "params": row.get("params") or {}, "enabled": row["enabled"], "note": row.get("note") or "",
        "used_by": model_registry.current().roles_of(row["id"], values),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _effective_values() -> dict:
    return {k: settings.get(k) for k in (*model_registry.ROLES, *model_registry.MODEL_LISTS)}


async def _unbind_model(engine, model_id: str) -> list[str]:
    """删除模型前解除所有引用:角色绑定回退、列表参数里移除。"""
    values = _effective_values()
    reset = [k for k in model_registry.ROLES if values.get(k) == model_id and settings.source(k) == "db"]
    update = {k: [v for v in values[k] if v != model_id] for k in model_registry.MODEL_LISTS
              if model_id in (values.get(k) or [])}
    if reset or update:
        await settings.save(engine.pg, update, reset)
    return [*reset, *update]


@router.get("/models")
async def list_models(engine=Depends(get_engine)):
    providers = {p["id"]: p for p in await engine.pg.list_providers()}
    values = _effective_values()
    return {"models": [_model_out(m, providers, values) for m in await engine.pg.list_models()]}


@router.post("/models")
async def create_model(body: ModelIn, request: Request, user: dict = Depends(require_admin),
                       engine=Depends(get_engine)):
    providers = {p["id"]: p for p in await engine.pg.list_providers()}
    try:
        mid = model_registry.check_id(body.id, "模型")
        if mid == model_registry.JUDGE_ALIAS:
            raise RegistryError(f"「{mid}」是保留名(始终指当前绑定的适用性判官),请换一个注册名")
        data = model_registry.check_model({**body.model_dump(), "id": mid}, providers)
    except RegistryError as e:
        raise _bad(str(e))
    if await engine.pg.get_model(mid):
        raise _bad(f"模型 {mid} 已存在", 409)
    row = await engine.pg.create_model(data)
    await audit(engine, user, "model.create", mid, {k: data[k] for k in ("provider_id", "kind", "model", "params")})
    await runtime.reload(request.app)
    return _model_out(row, providers, _effective_values())


@router.patch("/models/{model_id}")
async def update_model(model_id: str, body: ModelPatch, request: Request, user: dict = Depends(require_admin),
                       engine=Depends(get_engine)):
    current = await engine.pg.get_model(model_id)
    if current is None:
        raise _bad("模型不存在", 404)
    providers = {p["id"]: p for p in await engine.pg.list_providers()}
    fields = body.model_dump(exclude_unset=True)
    try:
        checked = model_registry.check_model({**current, **fields}, providers)
    except RegistryError as e:
        raise _bad(str(e))
    if "params" in fields:
        fields["params"] = checked["params"]
    if "model" in fields:
        fields["model"] = checked["model"]
    if "kind" in fields and fields["kind"] != current["kind"]:
        used = model_registry.current().roles_of(model_id, _effective_values())
        if used:
            raise _bad(f"模型正被 {', '.join(used)} 使用,不能更改类型")
    row = await engine.pg.update_model(model_id, fields)
    await audit(engine, user, "model.update", model_id, fields)
    await runtime.reload(request.app)
    return _model_out(row, providers, _effective_values())


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, request: Request, force: bool = False,
                       user: dict = Depends(require_admin), engine=Depends(get_engine)):
    if await engine.pg.get_model(model_id) is None:
        raise _bad("模型不存在", 404)
    used = model_registry.current().roles_of(model_id, _effective_values())
    if used and not force:
        raise _bad(f"模型正被使用:{', '.join(used)}。解除绑定后再删除,或强制删除(自动解除)。", 409)
    unbound = await _unbind_model(engine, model_id)
    await engine.pg.delete_model(model_id)
    await audit(engine, user, "model.delete", model_id, {"unbound": unbound})
    await runtime.reload(request.app)
    return {"status": "deleted", "id": model_id, "unbound": unbound}


@router.post("/models/{model_id}/test")
async def test_model(model_id: str, body: ModelTest, engine=Depends(get_engine)):
    row = await engine.pg.get_model(model_id)
    if row is None:
        raise _bad("模型不存在", 404)
    prow = await engine.pg.get_provider(row["provider_id"])
    if prow is None:
        raise _bad("服务商不存在", 404)
    return await model_registry.probe_model(model_registry.Model.from_row(row),
                                            model_registry.Provider.from_row(prow), body.prompt.strip())


@router.get("/models/roles")
async def get_roles(request: Request):
    values = {k: (settings.get(k) if settings.source(k) == "db" else "") for k in model_registry.ROLES}
    return {"roles": model_registry.role_status(request.app.state.config, model_registry.current(), values),
            "restart_required": runtime.restart_required(request.app)}


@router.put("/models/roles")
async def put_roles(body: RoleBindings, request: Request, user: dict = Depends(require_admin),
                    engine=Depends(get_engine)):
    unknown = [k for k in body.bindings if k not in model_registry.ROLES]
    if unknown:
        raise _bad(f"未知角色:{', '.join(unknown)}")
    values = {k: v for k, v in body.bindings.items() if v}
    reset = [k for k, v in body.bindings.items() if not v]
    _check_model_refs(values)
    changes = await settings.save(engine.pg, values, reset)
    if changes:
        await audit(engine, user, "model.bind", ", ".join(c["key"] for c in changes), {"changes": changes})
        await runtime.reload(request.app)
    return await get_roles(request)


@router.post("/models/import-env")
async def import_env(request: Request, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    """把环境变量里的旧模型配置(未绑定的角色 + 旧的老师配置)导入注册表并绑定。"""
    from app.config import Config, apply_runtime_settings, RUNTIME_SETTING_KEYS

    base = apply_runtime_settings(Config.from_env(), await engine.pg.get_settings(list(RUNTIME_SETTING_KEYS.values())))
    try:
        result = await model_registry.import_from_env(engine.pg, base)
    except Exception as e:
        raise _bad(f"导入失败:{e}")
    await audit(engine, user, "model.import_env", "", result)
    await runtime.reload(request.app)
    return result


# ── 安全与访问 ────────────────────────────────────────────────────

# Hook 接入片段里只放路径模板,不暴露部署机器的真实绝对路径
HOOK_SCRIPT_TEMPLATE = "<COGNITIVE_ENZYME_DIR>/hooks/ce_prime_hook.py"
_TOKEN_SETTINGS = {"api": "api_auth_token", "mcp": "mcp_auth_token"}


class SecurityUpdate(BaseModel):
    auth_enabled: bool


class TokenSet(BaseModel):
    token: str = ""


async def _security_public(engine) -> dict:
    from app.config import API_PORT, MCP_PORT
    from app.mcp_server import mcp_auth_active
    return {
        "ports": {"api": API_PORT, "mcp": MCP_PORT},   # 接入页按实际端口生成配置片段
        "auth_enabled": auth_enabled(),
        "api_token": await engine.pg.get_setting("api_auth_token", "") or "",
        "mcp_token": await engine.pg.get_setting("mcp_auth_token", "") or "",
        "mcp_auth_active": mcp_auth_active(),
        "hook_script": HOOK_SCRIPT_TEMPLATE,
    }


@router.get("/security")
async def get_security(engine=Depends(get_engine)):
    """访问认证开关与两枚机器令牌(静态 API 令牌给 hook / 园丁;MCP 令牌给 MCP 客户端)。"""
    return await _security_public(engine)


@router.patch("/security")
async def update_security(body: SecurityUpdate, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    from app.security import set_enabled
    await engine.pg.set_setting("auth_enabled", "true" if body.auth_enabled else "false")
    set_enabled(body.auth_enabled)
    await audit(engine, user, "security.auth", "", {"enabled": body.auth_enabled})
    return await _security_public(engine)


async def _apply_token(engine, kind: str, token: str) -> dict:
    """保存并应用令牌:API 令牌即时生效;MCP 令牌在已启用认证时即时生效,启用 / 关闭认证需重启。"""
    from app.mcp_server import update_mcp_token
    from app.security import configure_api_token
    if kind not in _TOKEN_SETTINGS:
        raise _bad("未知令牌类型", 404)
    await engine.pg.set_setting(_TOKEN_SETTINGS[kind], token)
    live = True
    if kind == "api":
        configure_api_token(token)
    else:
        live = update_mcp_token(token)
    return {**(await _security_public(engine)), "restart_required": not live}


@router.put("/security/tokens/{kind}")
async def set_token(kind: str, body: TokenSet, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    token = body.token.strip()
    if token and len(token) < 16:
        raise _bad("令牌至少 16 个字符")
    out = await _apply_token(engine, kind, token)
    await audit(engine, user, f"security.{kind}_token", "", {"action": "set" if token else "clear"})
    return out


@router.post("/security/tokens/{kind}/rotate")
async def rotate_token(kind: str, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    """服务端生成新令牌(旧令牌立即失效)。"""
    import secrets as _secrets
    out = await _apply_token(engine, kind, _secrets.token_urlsafe(32))
    await audit(engine, user, f"security.{kind}_token", "", {"action": "rotate"})
    return out


@router.delete("/security/tokens/{kind}")
async def clear_token(kind: str, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    out = await _apply_token(engine, kind, "")
    await audit(engine, user, f"security.{kind}_token", "", {"action": "clear"})
    return out


# ── 用户 ──────────────────────────────────────────────────────────

class UserIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"


class UserPatch(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


def _user_out(u: dict) -> dict:
    return {
        "id": u["id"], "username": u["username"], "display_name": u.get("display_name") or u["username"],
        "avatar_url": u.get("avatar_url") or "", "role": u.get("role") or "user",
        "is_active": bool(u.get("is_active", True)), "created_at": u.get("created_at"),
        "updated_at": u.get("updated_at"),
    }


@router.get("/users")
async def list_users(engine=Depends(get_engine)):
    return {"users": [_user_out(u) for u in await engine.pg.list_users()]}


@router.post("/users")
async def create_user(body: UserIn, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    username = body.username.strip()
    if not username or len(username) > 64:
        raise _bad("用户名不能为空,且不超过 64 个字符")
    if body.role not in _ROLES:
        raise _bad("角色只能是 admin 或 user")
    if len(body.password) < 8:
        raise _bad("密码至少 8 位")
    if await engine.pg.get_user_by_username(username):
        raise _bad("用户名已存在", 409)
    row = await engine.pg.create_user(user_id=uuid.uuid4().hex, username=username,
                                      password_hash=hash_password(body.password),
                                      display_name=body.display_name.strip() or username, role=body.role)
    await audit(engine, user, "user.create", username, {"role": body.role})
    return _user_out(row)


async def _guard_last_admin(engine, target: dict, *, demote: bool) -> None:
    if demote and target.get("role") == "admin" and target.get("is_active", True):
        if await engine.pg.count_active_admins() <= 1:
            raise _bad("至少需要保留一个启用的管理员")


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, user: dict = Depends(require_admin),
                      engine=Depends(get_engine)):
    target = await engine.pg.get_user_by_id(user_id)
    if target is None:
        raise _bad("用户不存在", 404)
    fields: dict[str, Any] = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name.strip() or target["username"]
    if body.role is not None:
        if body.role not in _ROLES:
            raise _bad("角色只能是 admin 或 user")
        if user_id == user.get("id") and body.role != "admin":
            raise _bad("不能取消自己的管理员身份")
        await _guard_last_admin(engine, target, demote=body.role != "admin")
        fields["role"] = body.role
    if body.is_active is not None:
        if user_id == user.get("id") and not body.is_active:
            raise _bad("不能停用自己")
        await _guard_last_admin(engine, target, demote=not body.is_active)
        fields["is_active"] = body.is_active
    if body.password:
        if len(body.password) < 8:
            raise _bad("密码至少 8 位")
        fields["password_hash"] = hash_password(body.password)
    row = await engine.pg.update_user(user_id, **fields)
    invalidate_user(user_id)  # 停用/改密/降权后,未过期 JWT 立即失效(auth_gate 缓存)
    detail = {k: v for k, v in fields.items() if k != "password_hash"}
    if "password_hash" in fields:
        detail["password"] = "reset"
    await audit(engine, user, "user.update", target["username"], detail)
    return _user_out(row)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    target = await engine.pg.get_user_by_id(user_id)
    if target is None:
        raise _bad("用户不存在", 404)
    if user_id == user.get("id"):
        raise _bad("不能删除自己")
    await _guard_last_admin(engine, target, demote=True)
    await engine.pg.delete_user(user_id)
    invalidate_user(user_id)
    await audit(engine, user, "user.delete", target["username"])
    return {"status": "deleted", "id": user_id}


# ── 审计日志 ──────────────────────────────────────────────────────

@router.get("/audit")
async def list_audit(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: str = "",
    actor: str = "",
    q: str = "",
    engine=Depends(get_engine),
):
    items, total = await engine.pg.list_audit(limit=limit, offset=offset, action=action, actor=actor, q=q)
    return {"items": items, "total": total}


# ── 后台任务 ──────────────────────────────────────────────────────

@router.get("/jobs")
async def list_jobs(
    kind: str = "",
    status: str = "",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    engine=Depends(get_engine),
):
    items, total = await engine.pg.list_jobs(kind=kind, status=status, limit=limit, offset=offset)
    return {"items": items, "total": total, "counts": await engine.pg.job_counts()}


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    if not await engine.pg.retry_job(job_id):
        raise _bad("只有失败且仍保留输入的任务可以重试", 409)
    await audit(engine, user, "job.retry", job_id)
    return {"status": "queued", "id": job_id}


# ── 系统状态 ──────────────────────────────────────────────────────

@router.get("/system")
async def system_status(request: Request, engine=Depends(get_engine)):
    from app import __version__ as version

    checks = {"postgresql": False, "qdrant": False, "mcp": False, "workers": False}
    info: dict[str, Any] = {}
    try:
        checks["postgresql"] = await engine.pg.ping()
        info["pg_version"] = await engine.pg.pg_version()
        info["schema_version"] = await engine.pg.schema_version()
        info["vector_sync_pending"] = await engine.pg.vector_sync_pending_count()
        info["jobs"] = await engine.pg.job_counts()
        info["tables"] = await engine.pg.table_sizes()
    except Exception as e:
        info["postgresql_error"] = str(e)
    try:
        checks["qdrant"] = await engine.qdrant.ping()
        info["qdrant"] = {"url": engine.qdrant._config.url, "collection": engine.qdrant.collection,
                          **(await engine.qdrant.get_stats())}
    except Exception as e:
        info["qdrant_error"] = str(e)
    mcp_server = getattr(request.app.state, "mcp_server", None)
    checks["mcp"] = bool(mcp_server and mcp_server.started)
    tasks = getattr(request.app.state, "background_tasks", [])
    checks["workers"] = bool(tasks) and all(not t.done() for t in tasks)

    sources = {"db": 0, "env": 0, "default": 0}
    for key in settings.SPECS:
        sources[settings.source(key)] += 1
    reg = model_registry.current()
    config = request.app.state.config
    return {
        "version": version,
        "python": sys.version.split()[0],
        "platform": platform.platform(terse=True),
        "started_at": _STARTED_AT.isoformat(),
        "uptime_s": int((datetime.now(timezone.utc) - _STARTED_AT).total_seconds()),
        "checks": checks,
        **info,
        "settings": sources,
        "registry": {"providers": len(reg.providers), "models": len(reg.models)},
        "capabilities": capabilities(config),
        "auth": {"enabled": auth_enabled(), "api_token_set": bool(await engine.pg.get_setting("api_auth_token", "")),
                 "mcp_token_set": bool(await engine.pg.get_setting("mcp_auth_token", ""))},
        "restart_required": runtime.restart_required(request.app),
    }


@router.post("/system/reindex")
async def reindex(user: dict = Depends(require_admin), engine=Depends(get_engine)):
    """把全部记忆重新排队写向量索引(换了向量模型之后用;维度变化需先重启)。"""
    queued = await engine.pg.enqueue_all_vector_sync()
    await audit(engine, user, "system.reindex", "", {"queued": queued})
    return {"queued": queued}


@router.post("/system/reload")
async def reload_config(request: Request, user: dict = Depends(require_admin), engine=Depends(get_engine)):
    await runtime.reload(request.app)
    await audit(engine, user, "system.reload")
    return {"status": "reloaded", "restart_required": runtime.restart_required(request.app)}


# ── 判定记录 ──────────────────────────────────────────────────────

@router.get("/distill/stats")
async def distill_stats(engine=Depends(get_engine)):
    import json as _json

    from app.distill.archive import archive_dir
    from app.distill.stats import collect

    stats = await collect(engine.pg)
    d = archive_dir()
    archive: dict[str, Any] = {"enabled": d is not None, "dir": str(d) if d else "",
                               "hours": settings.get("distill.archive_hours"),
                               "keep_days": settings.get("distill.keep_days")}
    if d is not None and (d / "state.json").exists():
        try:
            archive["state"] = _json.loads((d / "state.json").read_text(encoding="utf-8"))
            files = sorted(d.glob("*.jsonl.gz"))
            archive["files"] = len(files)
            archive["bytes"] = sum(f.stat().st_size for f in files)
        except Exception:
            pass
    return {**stats, "archive": archive, "decision_log": settings.get("distill.decision_log"),
            "shadow_judges": settings.get("distill.shadow_judges")}


@router.post("/distill/archive")
async def distill_archive_now(user: dict = Depends(require_admin), engine=Depends(get_engine)):
    from app.distill.archive import archive_dir, archive_once

    d = archive_dir()
    if d is None:
        raise _bad("归档已关闭(运行参数 distill.archive_dir 为空)", 409)
    result = await archive_once(engine.pg, d)
    await audit(engine, user, "distill.archive", str(d), result)
    return {"dir": str(d), **result}


@router.get("/distill/decisions")
async def list_decisions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str = "",
    judge: str = "",
    engine=Depends(get_engine),
):
    items, total = await engine.pg.list_decisions(limit=limit, offset=offset, source=source, judge=judge)
    return {"items": items, "total": total}


@router.get("/distill/decisions/{decision_id}")
async def get_decision(decision_id: str, engine=Depends(get_engine)):
    rec = await engine.pg.get_decision(decision_id)
    if rec is None:
        raise _bad("判定记录不存在", 404)
    return rec


# ── 召回调试台 ────────────────────────────────────────────────────

class PlaygroundPrime(BaseModel):
    context: str
    domain: str = ""
    project: str = ""
    max_results: int = Field(15, ge=1, le=50)
    hook_min_conf: float = Field(0.25, ge=0, le=1)


@router.post("/playground/prime")
async def playground_prime(body: PlaygroundPrime, engine=Depends(get_engine)):
    """与线上 prime 同一条流水线跑一遍,返回每个阶段的候选与分数——不写激活、不应期、判定记录。"""
    from app.core.features import build_search_query
    from app.matching.pipeline import run_pipeline

    context = body.context.strip()
    if not context:
        raise _bad("请输入情境")
    trace: dict = {}
    query = build_search_query(context, None)
    t0 = time.perf_counter()
    cfg = engine.config
    results = await run_pipeline(
        context=context, search_query=query, pg=engine.pg, qdrant=engine.qdrant, domain=body.domain or None,
        max_results=body.max_results, embedding_config=cfg.embedding, project=body.project.strip(), trace=trace,
        typesafe_config=cfg.typesafe, llm_config=cfg.llm, expand_config=cfg.expand_llm,
        judge_llm_config=cfg.judge_llm,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    recalled = trace.get("recalled_ids", [])
    injected = {r.enzyme.id: r for r in results}
    jev = {eid: (p, q) for eid, p, q in trace.get("jev", [])}
    ids = list(dict.fromkeys([*recalled, *jev, *injected]))
    enzymes = {e.id: e for e in await engine.pg.get_enzymes(ids)}
    rerank = dict(trace.get("rerank_raw", []))
    reranked = set(trace.get("reranked_ids", []))
    scored = {eid: (score, tier) for eid, score, tier in trace.get("scored", [])}
    analogy = {eid: (ap, aq) for eid, ap, aq in trace.get("analogy", [])}
    killed = set(trace.get("condition_killed_ids", []))
    lexical = set(trace.get("lexical_ids", []))
    hints = trace.get("hint_scores", {})
    rank = {eid: i + 1 for i, eid in enumerate(recalled)}
    candidates = []
    for eid in ids:
        e = enzymes.get(eid)
        r = injected.get(eid)
        p, q = jev.get(eid, (None, None))
        ap, aq = analogy.get(eid, (None, None))
        score, tier = scored.get(eid, (None, None))
        conf = e.confidence if e else None
        candidates.append({
            "id": eid, "patterns": e.patterns_readable if e else "", "then": e.then if e else "",
            "domain": e.domain if e else "", "severity": e.severity.value if e else "",
            "confidence": round(conf, 3) if conf is not None else None,
            "recall_rank": rank.get(eid), "lexical": eid in lexical, "hint": hints.get(eid),
            "condition_killed": eid in killed, "rerank": rerank.get(eid),
            "floor_passed": eid in reranked if rerank else None,
            "score": round(score, 4) if score is not None else None, "tier": tier,
            "p": p, "q": q, "analog_p": ap, "analog_q": aq,
            "injected": r is not None, "reason": r.reason if r else "",
            "exploratory": bool(r and r.exploratory),
            "hook_filtered": bool(r and not r.exploratory and conf is not None and conf < body.hook_min_conf),
        })
    order = {eid: i for i, eid in enumerate([*injected, *jev, *recalled])}
    candidates.sort(key=lambda c: (not c["injected"], order.get(c["id"], 1e9)))
    return {
        "query": query,
        "elapsed_ms": elapsed_ms,
        "status": {
            "judge": trace.get("jev_status", "off"), "judge_model": trace.get("jev_model", ""),
            "none": trace.get("jev_none"), "analogy_none": trace.get("analogy_none"),
            "llm_judge": trace.get("llm_judge_status", ""), "rerank_failed": trace.get("rerank_failed", False),
            "expansion": trace.get("expansion", ""), "judge_wait_ms": trace.get("jev_wait_ms"),
            "route": trace.get("route"),
        },
        "stages": {
            "recalled": len(recalled), "lexical": len(lexical), "condition_killed": len(killed),
            "reranked": len(reranked) if rerank else None, "scored": len(scored), "judged": len(jev),
            "injected": len(results),
        },
        "candidates": candidates,
    }
