"""后台管理相关测试共用的内存版 PGStore(只实现运行参数、模型注册表、审计与用户用到的方法)。"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone


class FakeAdminPG:
    def __init__(self, settings: dict[str, str] | None = None):
        self.settings: dict[str, tuple[str, datetime]] = {
            k: (v, datetime.now(timezone.utc)) for k, v in (settings or {}).items()}
        self.providers: dict[str, dict] = {}
        self.models: dict[str, dict] = {}
        self.audit_rows: list[dict] = []
        self.users: dict[str, dict] = {}
        self._ids = itertools.count(1)

    # app_settings
    async def get_setting(self, key, default=None):
        return self.settings[key][0] if key in self.settings else default

    async def set_setting(self, key, value):
        self.settings[key] = (value, datetime.now(timezone.utc))

    async def get_settings(self, keys):
        return {k: self.settings[k][0] for k in keys if k in self.settings}

    async def get_settings_with_prefix(self, prefix):
        return {k: v for k, v in self.settings.items() if k.startswith(prefix)}

    async def set_settings(self, values, delete=None):
        now = datetime.now(timezone.utc)
        for k, v in values.items():
            self.settings[k] = (v, now)
        for k in delete or []:
            self.settings.pop(k, None)

    async def delete_settings(self, keys):
        for k in keys:
            self.settings.pop(k, None)

    async def admin_fingerprint(self):
        return (len(self.settings), len(self.providers), len(self.models))

    # model registry
    async def list_providers(self):
        return [dict(p) for p in self.providers.values()]

    async def get_provider(self, pid):
        return dict(self.providers[pid]) if pid in self.providers else None

    async def create_provider(self, data):
        now = datetime.now(timezone.utc)
        row = {"name": "", "api_key": "", "headers": {}, "enabled": True, "note": "", **data,
               "created_at": now, "updated_at": now}
        self.providers[data["id"]] = row
        return dict(row)

    async def update_provider(self, pid, fields):
        if pid not in self.providers:
            return None
        self.providers[pid].update(fields)
        return dict(self.providers[pid])

    async def delete_provider(self, pid, cascade=False):
        if cascade:
            self.models = {k: v for k, v in self.models.items() if v["provider_id"] != pid}
        return self.providers.pop(pid, None) is not None

    async def list_models(self):
        return [dict(m) for m in self.models.values()]

    async def get_model(self, mid):
        return dict(self.models[mid]) if mid in self.models else None

    async def create_model(self, data):
        now = datetime.now(timezone.utc)
        row = {"params": {}, "enabled": True, "note": "", **data, "created_at": now, "updated_at": now}
        self.models[data["id"]] = row
        return dict(row)

    async def update_model(self, mid, fields):
        if mid not in self.models:
            return None
        self.models[mid].update(fields)
        return dict(self.models[mid])

    async def delete_model(self, mid):
        return self.models.pop(mid, None) is not None

    # audit
    async def audit(self, actor, action, target="", detail=None):
        self.audit_rows.append({"id": next(self._ids), "actor": actor, "action": action, "target": target,
                                "detail": detail or {}, "created_at": datetime.now(timezone.utc)})

    async def list_audit(self, limit=50, offset=0, action="", actor="", q=""):
        rows = [r for r in reversed(self.audit_rows)
                if (not action or r["action"] == action or r["action"].startswith(action + "."))
                and (not actor or r["actor"] == actor)]
        return rows[offset:offset + limit], len(rows)

    # users
    async def list_users(self):
        return list(self.users.values())

    async def get_user_by_id(self, uid):
        return self.users.get(uid)

    async def get_user_by_username(self, name):
        return next((u for u in self.users.values() if u["username"] == name), None)

    async def create_user(self, user_id, username, password_hash, display_name="", role="user"):
        self.users[user_id] = {"id": user_id, "username": username, "password_hash": password_hash,
                               "display_name": display_name or username, "avatar_url": "", "role": role,
                               "is_active": True, "created_at": None, "updated_at": None}
        return self.users[user_id]

    async def update_user(self, uid, **fields):
        self.users[uid].update({k: v for k, v in fields.items() if v is not None})
        return self.users[uid]

    async def delete_user(self, uid):
        return self.users.pop(uid, None) is not None

    async def count_active_admins(self):
        return sum(1 for u in self.users.values() if u["role"] == "admin" and u["is_active"])
