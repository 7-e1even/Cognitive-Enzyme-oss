"""Auth router — 登录与当前用户(资料、头像、密码)。安全设置在 /api/admin/security。"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

from app.dependencies import get_engine
from app.routers._helpers import audit
from app.security import (
    auth_enabled,
    decode_token,
    hash_password,
    invalidate_user,
    issue_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 头像存储目录（静态由 main.py 挂载在 /api/avatars）
AVATAR_DIR = Path(os.getenv("CE_DATA_DIR", "data")) / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
_ALLOWED_AVATAR_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MAX_AVATAR_BYTES = 4 * 1024 * 1024  # 4 MB

# 开发模式（未启用认证）下的伪用户，让 /me 等接口仍可用
_DEV_USER = {
    "id": "dev",
    "username": "dev",
    "display_name": "开发者",
    "avatar_url": "",
    "role": "admin",
}


def _public(user: dict) -> dict:
    """剔除敏感字段，返回前端可见的用户资料。"""
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "avatar_url": user.get("avatar_url") or "",
        "role": user.get("role") or "user",
    }


async def get_current_user(request: Request, engine=Depends(get_engine)) -> dict:
    if not auth_enabled():
        return _DEV_USER
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    claims = decode_token(token)
    if not claims:
        raise HTTPException(status_code=401, detail="未授权，请先登录")
    user = await engine.pg.get_user_by_id(claims["sub"])
    if not user or not user.get("is_active", True):
        raise HTTPException(status_code=401, detail="用户不存在或已被禁用")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ── Schemas ───────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class ProfileUpdate(BaseModel):
    display_name: str


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


# ── Endpoints ─────────────────────────────────────────────────

# 登录限速:同一 IP+用户名 5 分钟内最多失败 10 次(在线爆破的主要防线;
# 内存滑动窗口,重启清零,单实例部署足够)。
_LOGIN_WINDOW = 300.0
_LOGIN_MAX_FAILS = 10
_LOGIN_FAILS: dict[tuple[str, str], list[float]] = {}

# 用户不存在时的 dummy 校验对象(合法 bcrypt 哈希,内容是随机串)
_DUMMY_HASH = hash_password("ce-dummy-password-not-a-real-user")


def _login_throttled(key: tuple[str, str]) -> bool:
    fails = _LOGIN_FAILS.get(key)
    if not fails:
        return False
    now = time.monotonic()
    recent = [t for t in fails if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[key] = recent
    return len(recent) >= _LOGIN_MAX_FAILS


def _login_record_fail(key: tuple[str, str]) -> None:
    # 键总量受失败用户名×IP 组合数约束;超限时整表清空,防内存被刷大
    if len(_LOGIN_FAILS) > 10000:
        _LOGIN_FAILS.clear()
    _LOGIN_FAILS.setdefault(key, []).append(time.monotonic())


@router.get("/status")
async def auth_status():
    """公开端点：前端据此决定是否展示登录页。"""
    return {"auth_required": auth_enabled()}


@router.post("/login")
async def login(req: LoginRequest, request: Request, engine=Depends(get_engine)):
    if not auth_enabled():
        return {"auth_required": False, "token": "", "user": _public(_DEV_USER)}
    client = request.client.host if request.client else "unknown"
    throttle_key = (client, req.username.lower())
    if _login_throttled(throttle_key):
        raise HTTPException(status_code=429, detail="尝试次数过多,请稍后再试")
    user = await engine.pg.get_user_by_username(req.username)
    if not user:
        # 用户不存在也跑一遍 bcrypt,抹平"用户存在性"的响应时间差(防用户名枚举)
        verify_password(req.password, _DUMMY_HASH)
        _login_record_fail(throttle_key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not verify_password(req.password, user["password_hash"]):
        _login_record_fail(throttle_key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="账号已被禁用")
    _LOGIN_FAILS.pop(throttle_key, None)
    token, ttl = issue_token(user["id"], user["username"], user.get("role", "user"))
    return {"auth_required": True, "token": token, "expires_in": ttl, "user": _public(user)}


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return _public(user)


@router.patch("/me")
async def update_me(
    body: ProfileUpdate,
    user: dict = Depends(get_current_user),
    engine=Depends(get_engine),
):
    if user["id"] == "dev":
        raise HTTPException(status_code=400, detail="开发模式无账户可修改")
    updated = await engine.pg.update_user(user["id"], display_name=body.display_name.strip())
    return _public(updated)


@router.post("/me/password")
async def change_password(
    body: PasswordChange,
    user: dict = Depends(get_current_user),
    engine=Depends(get_engine),
):
    if user["id"] == "dev":
        raise HTTPException(status_code=400, detail="开发模式无账户可修改")
    if not verify_password(body.old_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="原密码错误")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    await engine.pg.update_user(user["id"], password_hash=hash_password(body.new_password))
    invalidate_user(user["id"])  # 改密后旧 token 立即失效(auth_gate 缓存)
    return {"ok": True}


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    engine=Depends(get_engine),
):
    if user["id"] == "dev":
        raise HTTPException(status_code=400, detail="开发模式无账户可修改")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_AVATAR_EXT:
        raise HTTPException(status_code=400, detail="仅支持 png/jpg/webp/gif 图片")
    data = await file.read()
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像不能超过 4MB")

    # 清理该用户旧头像（不同扩展名），再写新文件
    for old in AVATAR_DIR.glob(f"{user['id']}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    filename = f"{user['id']}{ext}"
    (AVATAR_DIR / filename).write_bytes(data)

    # 带版本号防浏览器缓存
    avatar_url = f"/api/avatars/{filename}?v={int(time.time())}"
    updated = await engine.pg.update_user(user["id"], avatar_url=avatar_url)
    return _public(updated)
