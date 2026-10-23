"""Authentication primitives — password hashing + JWT session tokens.

Authentication is **enabled by default**. The runtime flag and the JWT signing
secret live in the database (table ``app_settings``) and are loaded into module
state at startup via :func:`configure`. The flag can be toggled at runtime from
the admin settings page (see :func:`set_enabled`).

Multi-user ready: tokens carry the user id / username / role.
"""

from __future__ import annotations

import hmac as _hmac
import os
import time

import jwt
from passlib.context import CryptContext

_ALGO = "HS256"
TOKEN_TTL = 7 * 24 * 3600  # 7 天

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Runtime state, populated by configure() during app startup.
_STATE: dict = {
    "enabled": True,           # 默认启用认证
    "secret": "ce-dev-secret", # 启动时会被 DB/env 中的真实密钥覆盖
    "api_token": "",           # 静态机器令牌（供 hook 等无人值守客户端，不过期）
}


# ── Runtime configuration ─────────────────────────────────────

def configure(*, secret: str, enabled: bool) -> None:
    _STATE["secret"] = secret or _STATE["secret"]
    _STATE["enabled"] = bool(enabled)


def set_enabled(enabled: bool) -> None:
    _STATE["enabled"] = bool(enabled)


def configure_api_token(token: str) -> None:
    """设置静态 API 机器令牌（DB 为准，启动时载入）。空串=未启用。"""
    _STATE["api_token"] = (token or "").strip()


def api_token_valid(token: str) -> bool:
    """常量级比较：传入令牌是否等于已配置的静态机器令牌。"""
    configured = _STATE["api_token"]
    if not configured or not token:
        return False
    return _hmac.compare_digest(token, configured)


# The static token is copied into unattended hook / gardener environments, so it
# must remain a data-plane credential rather than a second administrator token.
_MACHINE_EXACT_ROUTES = {
    ("POST", "/api/prime"),
    ("POST", "/api/report"),
    ("POST", "/api/report_batch"),
    ("POST", "/api/harvest"),
    ("POST", "/api/capture"),
    ("POST", "/api/recall"),
    ("GET", "/api/enzymes"),       # gardener review input
    ("GET", "/api/metrics"),       # gardener closing summary
    ("POST", "/api/v1/systemone"),           # System 1 网关(给其他 agent,Jev 协议)
    ("POST", "/api/v1/systemone/feedback"),  # 网关判定的真实结果回传
}


def machine_api_access_allowed(method: str, path: str) -> bool:
    """Return whether the unattended static token may call an API route.

    Human JWTs still have access to the complete single-user control plane.
    """
    normalized_method = method.upper()
    if (normalized_method, path) in _MACHINE_EXACT_ROUTES:
        return True
    return normalized_method == "GET" and path.startswith("/api/episode/")


def auth_enabled() -> bool:
    """True 时 REST API 需要登录。默认开启，可在后台设置页关闭。"""
    return bool(_STATE["enabled"])


def seed_credentials() -> tuple[str, str]:
    """初始管理员账号:env 提供;密码留空返回空串,由调用方生成随机密码。

    不再默认 admin/admin:一键部署脚本常把服务暴露到公网,默认口令等于裸奔。
    """
    return (
        os.getenv("CE_ADMIN_USER", "").strip() or "admin",
        os.getenv("CE_ADMIN_PASSWORD", "").strip(),
    )


# ── Password hashing ──────────────────────────────────────────

def hash_password(password: str) -> str:
    return _pwd.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd.verify(password, password_hash)
    except Exception:
        return False


# ── JWT ───────────────────────────────────────────────────────

def _secret() -> str:
    return _STATE["secret"]


def issue_token(user_id: str, username: str, role: str = "user") -> tuple[str, int]:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO), TOKEN_TTL


def decode_token(token: str) -> dict | None:
    """返回 JWT claims；无效/过期返回 None。"""
    if not token:
        return None
    try:
        return jwt.decode(token, _secret(), algorithms=[_ALGO])
    except jwt.PyJWTError:
        return None


def verify_token(token: str) -> bool:
    return decode_token(token) is not None


# ── 活跃用户缓存(auth_gate 用)─────────────────────────────────
# 中间件每次请求都查库太贵(prime 是热路径),这里给 is_active 一个短 TTL 缓存;
# 停用/改密后经 invalidate_user 立即生效,否则最坏 TTL 秒内生效。
_USER_ACTIVE_TTL = 30.0
_USER_ACTIVE_CACHE: dict[str, tuple[bool, float]] = {}


def invalidate_user(user_id: str) -> None:
    """用户状态变更(停用/改密/删角色)后调用,下次请求立即重新查库。"""
    _USER_ACTIVE_CACHE.pop(user_id, None)


async def token_user_active(token: str, pg) -> bool:
    """JWT 有效且用户仍存在、未禁用。claims 无效或用户缺失一律 False。"""
    claims = decode_token(token)
    if not claims:
        return False
    user_id = claims.get("sub") or ""
    if not user_id:
        return False
    now = time.monotonic()
    cached = _USER_ACTIVE_CACHE.get(user_id)
    if cached and now - cached[1] < _USER_ACTIVE_TTL:
        return cached[0]
    try:
        user = await pg.get_user_by_id(user_id)
    except Exception:
        # 查库失败按"不可用"处理,宁可拒绝也不放大故障面
        return False
    active = bool(user and user.get("is_active", True))
    _USER_ACTIVE_CACHE[user_id] = (active, now)
    return active
