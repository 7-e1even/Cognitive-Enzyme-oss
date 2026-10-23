"""模型注册表:服务商(连接——协议、地址、密钥)与模型(服务商下的具体模型与参数),存数据库,
后台「模型」页管理。

- 协议只有三种:``openai``(Chat Completions / Embeddings / Rerank 兼容接口)、``anthropic``
  (Messages API)与 ``system_one``(TypeSafe 的 System One API:Jev、laya-serve)。
- 模型类型:``chat``(对话,openai / anthropic)、``embedding``、``rerank``(openai 兼容地址下的
  /embeddings、/rerank)、``judge``(system_one)。
- 各环节用哪个模型由角色绑定决定(运行参数 ``model.*``,见 app/settings.py);老师、影子判官与
  System 1 网关路由按模型注册名引用,``jev`` 始终指当前绑定的适用性判官。
- 未绑定的角色沿用旧配置(环境变量),与改造前一致;「从环境变量导入」把它们一键落进数据库。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.config import Config, EmbeddingConfig, LLMConfig, TypeSafeConfig, env_llm_variant

logger = logging.getLogger(__name__)

PROTOCOLS = ("openai", "anthropic", "system_one")
KINDS = ("chat", "embedding", "rerank", "judge")
KIND_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "chat": ("openai", "anthropic"),
    "embedding": ("openai",),
    "rerank": ("openai",),
    "judge": ("system_one",),
}
# 角色 → 允许的模型类型(与 app/settings.py 里 model.* 的 kinds 一致)
ROLES: dict[str, tuple[str, ...]] = {
    "model.llm": ("chat",),
    "model.embedding": ("embedding",),
    "model.rerank": ("rerank",),
    "model.judge": ("judge",),
    "model.query_expand": ("chat",),
    "model.judge_llm": ("chat",),
}
# 引用模型注册名的列表型参数
MODEL_LISTS = ("distill.shadow_judges", "gateway.route")
JUDGE_ALIAS = "jev"   # 永远指当前绑定的适用性判官(兼容旧的路由 / 影子判官写法)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    protocol: str
    base_url: str
    api_key: str = field(default="", repr=False)
    headers: dict = field(default_factory=dict, hash=False)
    enabled: bool = True
    note: str = ""

    @classmethod
    def from_row(cls, r: dict) -> "Provider":
        return cls(r["id"], r.get("name") or r["id"], r["protocol"], r.get("base_url") or "",
                   r.get("api_key") or "", dict(r.get("headers") or {}), bool(r.get("enabled", True)),
                   r.get("note") or "")


@dataclass(frozen=True)
class Model:
    id: str
    provider_id: str
    kind: str
    model: str
    params: dict = field(default_factory=dict, hash=False)
    enabled: bool = True
    note: str = ""

    @classmethod
    def from_row(cls, r: dict) -> "Model":
        return cls(r["id"], r["provider_id"], r["kind"], r["model"], dict(r.get("params") or {}),
                   bool(r.get("enabled", True)), r.get("note") or "")


def _num(value, cast, default):
    try:
        return cast(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def rerank_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    return base if base.endswith("/rerank") else f"{base}/rerank"


@dataclass(frozen=True)
class Registry:
    providers: dict[str, Provider] = field(default_factory=dict)
    models: dict[str, Model] = field(default_factory=dict)

    def resolve(self, model_id: str, kinds: tuple[str, ...] | None = None) -> tuple[Model, Provider] | None:
        """启用的模型 + 启用的服务商;类型不符或任一方停用都返回 None。"""
        m = self.models.get(model_id or "")
        if m is None or not m.enabled or (kinds and m.kind not in kinds):
            return None
        p = self.providers.get(m.provider_id)
        if p is None or not p.enabled or p.protocol not in KIND_PROTOCOLS.get(m.kind, ()):
            return None
        return m, p

    def llm_config(self, model_id: str) -> LLMConfig | None:
        hit = self.resolve(model_id, ("chat",))
        if hit is None:
            return None
        m, p = hit
        base = LLMConfig()
        return LLMConfig(
            api_key=p.api_key, api_url=p.base_url, model=m.model,
            # 参数里显式写 null = 不传温度(部分推理模型只接受默认温度)
            temperature=(m.params["temperature"] if "temperature" in m.params else base.temperature),
            max_tokens=_num(m.params.get("max_tokens"), int, base.max_tokens),
            timeout=_num(m.params.get("timeout"), float, base.timeout),
            protocol=p.protocol, extra=dict(m.params.get("extra") or {}), headers=dict(p.headers),
        )

    def typesafe_config(self, model_id: str) -> TypeSafeConfig | None:
        hit = self.resolve(model_id, ("judge",))
        if hit is None:
            return None
        m, p = hit
        return TypeSafeConfig(api_key=p.api_key, api_url=p.base_url, model=m.model)

    def roles_of(self, model_id: str, values: dict) -> list[str]:
        """这个模型被哪些角色 / 列表参数引用(删除前提示用)。"""
        out = [k for k in ROLES if values.get(k) == model_id]
        out += [k for k in MODEL_LISTS if model_id in (values.get(k) or [])]
        return out


_current = Registry()
_version = 0
_listeners: list[Callable[[], None]] = []


def current() -> Registry:
    return _current


def version() -> int:
    return _version


def on_change(callback: Callable[[], None]) -> None:
    """注册表变化时的回调(清老师实例缓存等)。"""
    _listeners.append(callback)


def set_current(registry: Registry) -> None:
    global _current, _version
    _current = registry
    _version += 1
    for cb in list(_listeners):
        try:
            cb()
        except Exception:
            logger.debug("registry listener failed", exc_info=True)


async def fetch(pg) -> Registry:
    """读数据库里的注册表(不动进程状态)。库还没升级(表不存在,如评测脚本读旧版生产库)时返回空表。"""
    try:
        providers = {r["id"]: Provider.from_row(r) for r in await pg.list_providers()}
        models = {r["id"]: Model.from_row(r) for r in await pg.list_models()}
    except Exception as e:
        if type(e).__name__ == "UndefinedTableError":
            return Registry()
        raise
    return Registry(providers, models)


async def load(pg) -> Registry:
    reg = await fetch(pg)
    set_current(reg)
    return reg


# ── 角色解析:数据库绑定覆盖环境变量 ──────────────────────────────

def apply_roles(config: Config, reg: Registry, values: dict) -> Config:
    """把角色绑定落到运行配置上;未绑定(或绑定的模型不可用)的角色保持原配置(环境变量)。"""
    llm = reg.llm_config(values.get("model.llm", "")) or config.llm
    emb = config.embedding
    hit = reg.resolve(values.get("model.embedding", ""), ("embedding",))
    if hit:
        m, p = hit
        emb = replace(emb, api_key=p.api_key, api_url=p.base_url, model=m.model,
                      dimensions=_num(m.params.get("dimensions"), int, emb.dimensions))
    hit = reg.resolve(values.get("model.rerank", ""), ("rerank",))
    if hit:
        m, p = hit
        emb = replace(emb, rerank_api_url=rerank_url(p.base_url), rerank_model=m.model,
                      rerank_top_n=_num(m.params.get("top_n"), int, emb.rerank_top_n),
                      rerank_api_key=p.api_key)
    typesafe = reg.typesafe_config(values.get("model.judge", "")) or config.typesafe
    # 未绑定时沿用旧环境变量写法:在最终生效的引擎 LLM 连接上换模型名(与改造前一致)
    expand = (reg.llm_config(values.get("model.query_expand", ""))
              or env_llm_variant(llm, "CE_QUERY_EXPAND"))
    judge_llm = reg.llm_config(values.get("model.judge_llm", "")) or env_llm_variant(llm, "CE_JUDGE_LLM")
    return replace(config, llm=llm, embedding=emb, typesafe=typesafe, expand_llm=expand, judge_llm=judge_llm)


def role_status(config: Config, reg: Registry, values: dict) -> list[dict]:
    """每个角色当前生效的是谁:db(绑定的模型)| env(环境变量旧配置)| none(未配置)。"""
    from app import settings

    def env_summary(role: str) -> dict | None:
        if role == "model.llm" and config.llm.available:
            return {"model": config.llm.model, "url": config.llm.api_url}
        if role == "model.embedding" and config.embedding.api_key and config.embedding.api_url:
            return {"model": config.embedding.model, "url": config.embedding.api_url}
        if role == "model.rerank" and config.embedding.rerank_api_url and (
                config.embedding.rerank_api_key or config.embedding.api_key):
            return {"model": config.embedding.rerank_model, "url": config.embedding.rerank_api_url}
        if role == "model.judge" and config.typesafe.available:
            return {"model": config.typesafe.model, "url": config.typesafe.api_url}
        if role == "model.query_expand" and config.expand_llm is not None:
            return {"model": config.expand_llm.model, "url": config.expand_llm.api_url}
        if role == "model.judge_llm" and config.judge_llm is not None:
            return {"model": config.judge_llm.model, "url": config.judge_llm.api_url}
        return None

    out = []
    for role, kinds in ROLES.items():
        spec = settings.SPECS[role]
        bound = values.get(role, "")
        hit = reg.resolve(bound, kinds) if bound else None
        if hit:
            m, p = hit
            status = {"source": "db", "model": m.model, "url": p.base_url, "provider": p.name}
        elif (env := env_summary(role)) is not None and not bound:
            status = {"source": "env", **env}
        elif role in ("model.query_expand", "model.judge_llm") and not bound:
            status = {"source": "inherit"}   # 未单独指定:用引擎 LLM
        else:
            status = {"source": "none"}
        if bound and not hit:
            status["error"] = "绑定的模型不存在、已停用或类型不符"
        out.append({"key": role, "label": spec.label, "help": spec.help, "kinds": list(kinds),
                    "advanced": spec.advanced, "model_id": bound, **status})
    return out


# ── 校验 ──────────────────────────────────────────────────────────

class RegistryError(ValueError):
    pass


def check_id(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _ID_RE.match(value):
        raise RegistryError(f"{what} ID 只能包含字母、数字和 . _ : -,以字母或数字开头,最长 64 位")
    return value


def check_provider(data: dict) -> dict:
    protocol = data.get("protocol")
    if protocol not in PROTOCOLS:
        raise RegistryError(f"协议必须是 {' / '.join(PROTOCOLS)} 之一")
    url = (data.get("base_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise RegistryError("接口地址需以 http:// 或 https:// 开头")
    headers = data.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in headers.items()):
        raise RegistryError("额外请求头必须是 字符串 → 字符串 的对象")
    return {**data, "base_url": url.rstrip("/"), "headers": headers}


_PARAM_TYPES: dict[str, dict[str, type]] = {
    "chat": {"temperature": float, "max_tokens": int, "timeout": float, "concurrency": int, "retries": int,
             "api_version": str, "extra": dict},
    "embedding": {"dimensions": int},
    "rerank": {"top_n": int},
    "judge": {"timeout": float, "concurrency": int, "retries": int},
}


def check_model(data: dict, providers: dict[str, dict]) -> dict:
    kind = data.get("kind")
    if kind not in KINDS:
        raise RegistryError(f"模型类型必须是 {' / '.join(KINDS)} 之一")
    provider = providers.get(data.get("provider_id") or "")
    if provider is None:
        raise RegistryError("服务商不存在")
    if provider["protocol"] not in KIND_PROTOCOLS[kind]:
        raise RegistryError(f"{kind} 模型需要 {' / '.join(KIND_PROTOCOLS[kind])} 协议的服务商,"
                            f"该服务商是 {provider['protocol']}")
    if not (data.get("model") or "").strip():
        raise RegistryError("请填写服务商侧的模型名")
    params: dict = {}
    for key, value in (data.get("params") or {}).items():
        typ = _PARAM_TYPES[kind].get(key)
        if typ is None:
            continue
        if key == "temperature" and value is None:
            params[key] = None          # 显式不传温度
            continue
        if value is None or value == "":
            continue
        try:
            if typ is dict:
                if not isinstance(value, dict):
                    raise TypeError
                params[key] = value
            elif typ is str:
                params[key] = str(value)
            else:
                params[key] = typ(value)
        except (TypeError, ValueError):
            raise RegistryError(f"参数 {key} 的值不合法") from None
    if kind == "embedding" and params.get("dimensions", 1) <= 0:
        raise RegistryError("向量维度必须大于 0")
    return {**data, "model": data["model"].strip(), "params": params}


# ── 从旧配置迁移 / 导入 ───────────────────────────────────────────

def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._:-]+", "-", text or "").strip("-.")
    return (s or fallback)[:64]


def _provider_slug(url: str, fallback: str) -> str:
    host = urlparse(url).hostname or ""
    parts = [p for p in host.split(".") if p and p not in ("api", "www", "open")]
    if not parts:
        return fallback
    if host in ("localhost", "127.0.0.1"):
        port = urlparse(url).port
        return _slug(f"local-{port}" if port else "local", fallback)
    return _slug(parts[-2] if len(parts) >= 2 else parts[0], fallback)


class _Importer:
    """把「连接 + 模型」落进注册表:同协议同地址同密钥的服务商复用,注册名冲突时加后缀。"""

    def __init__(self, pg, providers: list[dict], models: list[dict]):
        self.pg = pg
        self.providers = {p["id"]: p for p in providers}
        self.models = {m["id"]: m for m in models}
        self.created: list[str] = []

    async def provider(self, protocol: str, base_url: str, api_key: str, hint: str, name: str = "",
                       headers: dict | None = None) -> str:
        base_url = base_url.rstrip("/")
        for p in self.providers.values():
            if p["protocol"] == protocol and p["base_url"].rstrip("/") == base_url and p["api_key"] == api_key:
                return p["id"]
        pid = base = _provider_slug(base_url, hint)
        n = 2
        while pid in self.providers:
            pid, n = f"{base}-{n}", n + 1
        row = await self.pg.create_provider({"id": pid, "name": name or pid, "protocol": protocol,
                                             "base_url": base_url, "api_key": api_key,
                                             "headers": headers or {}, "note": "从旧配置导入"})
        self.providers[pid] = row
        self.created.append(f"provider:{pid}")
        return pid

    async def model(self, provider_id: str, kind: str, model: str, params: dict, name: str = "") -> str:
        for m in self.models.values():
            if m["provider_id"] == provider_id and m["kind"] == kind and m["model"] == model:
                return m["id"]
        mid = base = _slug(name or model.split("/")[-1], kind)
        n = 2
        while mid in self.models:
            mid, n = f"{base}-{n}", n + 1
        row = await self.pg.create_model({"id": mid, "provider_id": provider_id, "kind": kind, "model": model,
                                          "params": params, "note": "从旧配置导入"})
        self.models[mid] = row
        self.created.append(f"model:{mid}")
        return mid


async def _bindings_from_config(imp: _Importer, config: Config, want: set[str]) -> dict[str, str]:
    bind: dict[str, str] = {}
    if "model.llm" in want and config.llm.available:
        pid = await imp.provider(config.llm.protocol, config.llm.api_url, config.llm.api_key, "llm")
        bind["model.llm"] = await imp.model(pid, "chat", config.llm.model, {
            "temperature": config.llm.temperature, "max_tokens": config.llm.max_tokens,
            **({"extra": config.llm.extra} if config.llm.extra else {})})
    emb = config.embedding
    if "model.embedding" in want and emb.api_key and emb.api_url:
        pid = await imp.provider("openai", emb.api_url, emb.api_key, "embedding")
        bind["model.embedding"] = await imp.model(pid, "embedding", emb.model, {"dimensions": emb.dimensions})
    rerank_key = emb.rerank_api_key or emb.api_key
    if "model.rerank" in want and emb.rerank_api_url and rerank_key:
        base = emb.rerank_api_url.rstrip("/")
        base = base[: -len("/rerank")] if base.endswith("/rerank") else base
        pid = await imp.provider("openai", base, rerank_key, "rerank")
        bind["model.rerank"] = await imp.model(pid, "rerank", emb.rerank_model, {"top_n": emb.rerank_top_n})
    if "model.judge" in want and config.typesafe.available:
        pid = await imp.provider("system_one", config.typesafe.api_url, config.typesafe.api_key, "typesafe",
                                 name="TypeSafe")
        bind["model.judge"] = await imp.model(pid, "judge", config.typesafe.model, {})
    for role, cfg in (("model.query_expand", config.expand_llm), ("model.judge_llm", config.judge_llm)):
        if role in want and cfg is not None and cfg.available:
            pid = await imp.provider(cfg.protocol, cfg.api_url, cfg.api_key, "llm")
            bind[role] = await imp.model(pid, "chat", cfg.model, {"extra": cfg.extra} if cfg.extra else {})
    return bind


LEGACY_MODEL_KEYS = (
    "runtime.llm.api_key", "runtime.llm.api_url", "runtime.llm.model", "runtime.llm.temperature",
    "runtime.llm.max_tokens", "runtime.embedding.api_key", "runtime.embedding.api_url",
    "runtime.embedding.model", "runtime.embedding.dimensions", "runtime.rerank.api_url",
    "runtime.rerank.model", "runtime.rerank.top_n", "runtime.typesafe.api_key", "runtime.typesafe.api_url",
    "runtime.typesafe.model",
)
LEGACY_SYSTEM_KEYS = {
    "runtime.qdrant.url": "system.qdrant_url",
    "runtime.qdrant.collection": "system.qdrant_collection",
    "runtime.paths.rules_path": "system.rules_path",
    "runtime.paths.workspace": "system.workspace",
}


async def migrate_legacy(pg) -> list[str]:
    """一次性迁移旧版「运行配置」页写入的 runtime.* 键:模型连接 → 服务商 / 模型 + 角色绑定,
    向量库与路径 → 运行参数。幂等:迁移完删除旧键,之后再调用什么都不做。"""
    import json

    from app import settings
    from app.config import apply_runtime_settings

    keys = [*LEGACY_MODEL_KEYS, *LEGACY_SYSTEM_KEYS]
    legacy = await pg.get_settings(keys)
    if not legacy:
        return []
    done: list[str] = []
    values = await settings.read(pg)
    if any(k in legacy for k in LEGACY_MODEL_KEYS):
        # 迁移升级前实际生效的配置(旧键叠在环境变量上,与旧版 load_runtime_config 一致),
        # 只迁移数据库里确实存过的角色
        effective = apply_runtime_settings(Config.from_env(), legacy)
        present = {role for role, prefix in (("model.llm", "runtime.llm."), ("model.embedding", "runtime.embedding."),
                                             ("model.rerank", "runtime.rerank."), ("model.judge", "runtime.typesafe."))
                   if any(k.startswith(prefix) for k in legacy)}
        imp = _Importer(pg, await pg.list_providers(), await pg.list_models())
        bind = await _bindings_from_config(imp, effective, {r for r in present if not values.get(r)})
        if bind:
            await pg.set_settings({settings.PREFIX + k: json.dumps(v) for k, v in bind.items()})
        done += imp.created + [f"bind:{k}={v}" for k, v in bind.items()]
    system = {new: legacy[old] for old, new in LEGACY_SYSTEM_KEYS.items()
              if legacy.get(old) and new not in values}
    if system:
        await pg.set_settings({settings.PREFIX + k: json.dumps(v) for k, v in system.items()})
        done += [f"setting:{k}" for k in system]
    await pg.delete_settings(list(legacy))
    logger.info("Migrated legacy runtime settings: %s", done)
    return done


async def import_from_env(pg, config: Config) -> dict:
    """把环境变量里的旧配置(未绑定的角色 + CE_TEACHERS_FILE / CE_TEACHERS 里的老师)导入注册表。"""
    import json

    from app import settings
    from app.distill.teachers import env_specs

    values = await settings.read(pg)
    imp = _Importer(pg, await pg.list_providers(), await pg.list_models())
    want = {role for role in ROLES if not values.get(role)}
    bind = await _bindings_from_config(imp, config, want)
    teachers = []
    for spec in env_specs():
        key = spec.api_key or ""
        if spec.kind == "system_one":
            pid = await imp.provider("system_one", spec.url, key, spec.name, headers=spec.headers)
            params = {"timeout": spec.timeout, "concurrency": spec.concurrency, "retries": spec.retries}
            teachers.append(await imp.model(pid, "judge", spec.model, params, name=spec.name))
        else:
            pid = await imp.provider(spec.kind, spec.url, key, spec.name, headers=spec.headers)
            params = {"max_tokens": spec.max_tokens, "timeout": spec.timeout, "concurrency": spec.concurrency,
                      "retries": spec.retries, **({"temperature": spec.temperature}
                                                  if spec.temperature is not None else {}),
                      **({"extra": spec.extra} if spec.extra else {}),
                      **({"api_version": spec.api_version} if spec.kind == "anthropic" else {})}
            teachers.append(await imp.model(pid, "chat", spec.model, params, name=spec.name))
    if bind:
        await pg.set_settings({settings.PREFIX + k: json.dumps(v) for k, v in bind.items()})
    return {"created": imp.created, "bindings": bind, "teachers": teachers}


# ── 连通性测试 ────────────────────────────────────────────────────

def _err(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        body = e.response.text[:300].strip()
        return f"HTTP {e.response.status_code}" + (f": {body}" if body else "")
    return f"{type(e).__name__}: {e}"[:400]


async def probe_provider(p: Provider) -> dict:
    """服务商连通性:openai / anthropic 列出可用模型;system_one 没有列表接口,只测能否建立连接。"""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if p.protocol == "openai":
                resp = await client.get(f"{p.base_url}/models",
                                        headers={"Authorization": f"Bearer {p.api_key}", **p.headers})
            elif p.protocol == "anthropic":
                base = p.base_url[: -len("/v1")] if p.base_url.endswith("/v1") else p.base_url
                resp = await client.get(f"{base}/v1/models", headers={
                    "x-api-key": p.api_key, "anthropic-version": "2023-06-01", **p.headers})
            else:
                resp = await client.get(p.base_url, headers={"Authorization": f"Bearer {p.api_key}"})
                return {"ok": resp.status_code < 500, "latency_ms": round((time.perf_counter() - t0) * 1000),
                        "detail": f"HTTP {resp.status_code}(System One 协议没有模型列表,请在模型上测试)",
                        "models": []}
            resp.raise_for_status()
            data = resp.json()
        ids = sorted({str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and m.get("id")})
        return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000),
                "detail": f"连接成功,可用模型 {len(ids)} 个", "models": ids[:500]}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000), "detail": _err(e),
                "models": []}


async def probe_model(m: Model, p: Provider, prompt: str = "") -> dict:
    """按模型类型发一次最小请求:对话回一句话、向量返回维度、精排给两段文本打分、判官答一道是否题。"""
    from app.llm_client import complete
    from app.matching.rerank import rerank

    t0 = time.perf_counter()

    def done(ok: bool, detail: str, **extra) -> dict:
        return {"ok": ok, "latency_ms": round((time.perf_counter() - t0) * 1000), "detail": detail, **extra}

    try:
        if m.kind == "chat":
            reg = Registry({p.id: p}, {m.id: m})
            cfg = reg.llm_config(m.id)
            if cfg is None:
                return done(False, "模型或服务商已停用")
            text = await complete(cfg, [{"role": "user", "content": prompt or "用一句话介绍你自己。"}],
                                  system="You are a connectivity test. Reply briefly.",
                                  max_tokens=min(cfg.max_tokens, 512), timeout=60.0, raise_errors=True)
            return done(bool(text), "已收到回复" if text else "模型没有返回内容", response=text or "")
        if m.kind == "embedding":
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{p.base_url}/embeddings",
                                         headers={"Authorization": f"Bearer {p.api_key}"},
                                         json={"model": m.model, "input": prompt or "connectivity test"})
                resp.raise_for_status()
                vec = resp.json()["data"][0]["embedding"]
            want = m.params.get("dimensions")
            note = "" if not want or int(want) == len(vec) else f"(注意:配置的维度是 {want})"
            return done(not note, f"返回 {len(vec)} 维向量{note}", dimensions=len(vec))
        if m.kind == "rerank":
            cfg = EmbeddingConfig(api_key=p.api_key, rerank_api_key=p.api_key, rerank_api_url=rerank_url(p.base_url),
                                  rerank_model=m.model)
            query = prompt or "如何避免 SQL 注入?"
            docs = ["使用参数化查询,不要拼接用户输入到 SQL 语句里。", "今天的天气适合出门散步。"]
            results = await rerank(query, docs, cfg, top_n=2)
            if not results:
                return done(False, "精排接口没有返回结果(检查地址、密钥与模型名)")
            scores = {r.index: round(r.score, 4) for r in results}
            return done(True, f"相关文本得分 {scores.get(0)},无关文本得分 {scores.get(1)}",
                        scores=[scores.get(0), scores.get(1)])
        if m.kind == "judge":
            from app.typesafe_client import system_one
            cfg = TypeSafeConfig(api_key=p.api_key, api_url=p.base_url, model=m.model)
            # 判官单次请求有 token 硬上限:自定义 prompt 截断,别把连通性测试打成 422。
            questions = {"q": {"type": "noul", "instructions": (prompt or "state 描述的是一个编程问题吗?")[:2000],
                               "criteria": {"true": "是编程问题", "false": "不是编程问题"}}}
            data = await system_one("我的 Python 脚本读取 CSV 时报 UnicodeDecodeError。", questions, cfg,
                                    timeout=30.0)
            if not data or "answers" not in data:
                return done(False, "判官没有返回结果(检查地址、密钥与模型名)")
            prob = (data["answers"].get("q") or {}).get("noul")
            return done(prob is not None, f"判定概率 {prob}", answers=data["answers"], model=data.get("model"))
        return done(False, f"未知模型类型 {m.kind}")
    except Exception as e:
        return done(False, _err(e))
