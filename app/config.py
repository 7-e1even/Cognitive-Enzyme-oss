"""运行配置:连接与模型的解析结果(热路径读取的快照)。

启动引导值(数据库连接等)来自环境变量;模型与运行参数由后台管理(见 app/model_registry.py、
app/settings.py),:func:`load_runtime_config` 把它们叠加到环境变量之上。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Mapping

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """数值型环境变量:坏值给清晰日志并回落默认,而不是模块加载时裸 ValueError。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数,使用默认值 %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数值,使用默认值 %s", name, raw, default)
        return default


@dataclass(frozen=True)
class PGConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "cognitive_enzyme"
    user: str = "enzyme"
    password: str = "enzyme"

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass(frozen=True)
class EmbeddingConfig:
    api_key: str = ""
    api_url: str = ""
    # 默认对与 from_env/llm_client 兜底保持一致(text-embedding-3-small=1536 维);
    # 真实部署值来自 .env/app_settings,这里只保证"默认组合自洽"不埋部署坑。
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    rerank_api_url: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_top_n: int = 15
    rerank_api_key: str = ""   # 留空沿用向量模型的密钥(旧配置里两者共用一把 key)



# 监听端口:启动参数,和数据库连接一样只从环境变量读。默认避开常见的开发端口(3000、8000、8080……)
API_PORT = _env_int("CE_API_PORT", 8420)   # REST API
MCP_PORT = _env_int("CE_MCP_PORT", 8421)   # MCP(Streamable HTTP,/mcp)

@dataclass(frozen=True)
class LLMConfig:
    api_key: str = ""
    api_url: str = ""     # e.g. https://api.openai.com/v1
    model: str = "gpt-4o-mini"
    temperature: float | None = 0.7    # None = 不传(部分推理模型只接受默认温度)
    max_tokens: int = 1024
    timeout: float = 0.0   # 秒;0 = 用调用处的默认值。思考模型在「模型」页按模型设大一些
    protocol: str = "openai"                                  # openai | anthropic
    extra: dict = field(default_factory=dict, hash=False)     # 额外请求体字段(如关闭思考)
    headers: dict = field(default_factory=dict, hash=False)   # 额外请求头

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_url)


@dataclass(frozen=True)
class TypeSafeConfig:
    """TypeSafe System One(Jev)—— prime 热路径上的记忆适用性判定(见 app/typesafe_client.py)。
    模型钉版本而非 jev-latest:阈值按版本标定,别名漂移会让阈值静默失准。"""
    api_key: str = ""
    api_url: str = "https://api.typesafe.ai/v1"
    model: str = "jev-1.13.0"

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.api_url)


@dataclass(frozen=True)
class QdrantConfig:
    url: str = "http://localhost:6333"
    collection_name: str = "enzymes"


@dataclass(frozen=True)
class Config:
    pg: PGConfig = field(default_factory=PGConfig)
    qdrant: QdrantConfig = field(default_factory=QdrantConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    typesafe: TypeSafeConfig = field(default_factory=TypeSafeConfig)
    rules_path: str = ""  # 写 Rules 文件的路径
    workspace: str = ""   # 当前工作目录
    expand_llm: LLMConfig | None = None   # 查询扩写用的模型;None = 引擎 LLM
    judge_llm: LLMConfig | None = None    # LLM 第二判官用的模型;None = 引擎 LLM

    @classmethod
    def from_env(cls) -> Config:
        config = cls(
            pg=PGConfig(
                host=os.getenv("CE_PG_HOST", "localhost"),
                port=_env_int("CE_PG_PORT", 5432),
                database=os.getenv("CE_PG_DB", "cognitive_enzyme"),
                user=os.getenv("CE_PG_USER", "enzyme"),
                password=os.getenv("CE_PG_PASSWORD", "enzyme"),
            ),
            embedding=EmbeddingConfig(
                api_key=os.getenv("EMBEDDING_API_KEY", ""),
                api_url=os.getenv("EMBEDDING_API_URL", ""),
                model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                dimensions=_env_int("EMBEDDING_DIMENSIONS", 1536),
                rerank_api_url=os.getenv("RERANK_API_URL", ""),
                rerank_model=os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
                rerank_top_n=_env_int("RERANK_TOP_N", 15),
            ),
            qdrant=QdrantConfig(
                url=os.getenv("CE_QDRANT_URL", "http://localhost:6333"),
                collection_name=os.getenv("CE_QDRANT_COLLECTION", "enzymes"),
            ),
            llm=LLMConfig(
                api_key=os.getenv("CE_LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
                api_url=os.getenv("CE_LLM_API_URL", os.getenv("OPENAI_API_URL", "https://api.openai.com/v1")),
                model=os.getenv("CE_LLM_MODEL", "gpt-4o-mini"),
                temperature=_env_float("CE_LLM_TEMPERATURE", 0.7),
                max_tokens=_env_int("CE_LLM_MAX_TOKENS", 1024),
            ),
            typesafe=TypeSafeConfig(
                api_key=os.getenv("TYPESAFE_API_KEY", ""),
                api_url=os.getenv("TYPESAFE_API_URL", "https://api.typesafe.ai/v1"),
                model=os.getenv("TYPESAFE_MODEL", "jev-1.13.0"),
            ),
            rules_path=os.getenv("CE_RULES_PATH", ""),
            workspace=os.getenv("CE_WORKSPACE", os.getcwd()),
        )
        return with_env_variants(config)


def with_env_variants(config: Config) -> Config:
    """旧的「单独换模型」环境变量(CE_QUERY_EXPAND_MODEL / _EXTRA、CE_JUDGE_LLM_MODEL / _EXTRA):
    在(最终生效的)引擎 LLM 连接上换模型名与额外请求体,与改造前 expand / llm_judge 里的行为一致。"""
    return replace(config, expand_llm=env_llm_variant(config.llm, "CE_QUERY_EXPAND"),
                   judge_llm=env_llm_variant(config.llm, "CE_JUDGE_LLM"))


def env_llm_variant(llm: LLMConfig, prefix: str) -> LLMConfig | None:
    model = os.getenv(f"{prefix}_MODEL", "").strip()
    raw = os.getenv(f"{prefix}_EXTRA", "").strip()
    extra: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            extra = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            logger.warning("%s_EXTRA is not valid JSON, ignored", prefix)
    if not model and not extra:
        return None
    return replace(llm, model=model or llm.model, extra={**llm.extra, **extra})


# 旧版「运行配置」页写入的键:启动时由 model_registry.migrate_legacy 迁移成服务商 / 模型 + 运行参数,
# 这里只为迁移前的库(以及评测脚本直接读取生产库)保留解析。
RUNTIME_SETTING_KEYS = {
    "llm_api_key": "runtime.llm.api_key",
    "llm_api_url": "runtime.llm.api_url",
    "llm_model": "runtime.llm.model",
    "llm_temperature": "runtime.llm.temperature",
    "llm_max_tokens": "runtime.llm.max_tokens",
    "embedding_api_key": "runtime.embedding.api_key",
    "embedding_api_url": "runtime.embedding.api_url",
    "embedding_model": "runtime.embedding.model",
    "embedding_dimensions": "runtime.embedding.dimensions",
    "rerank_api_url": "runtime.rerank.api_url",
    "rerank_model": "runtime.rerank.model",
    "rerank_top_n": "runtime.rerank.top_n",
    "typesafe_api_key": "runtime.typesafe.api_key",
    "typesafe_api_url": "runtime.typesafe.api_url",
    "typesafe_model": "runtime.typesafe.model",
    "qdrant_url": "runtime.qdrant.url",
    "qdrant_collection": "runtime.qdrant.collection",
    "rules_path": "runtime.paths.rules_path",
    "workspace": "runtime.paths.workspace",
}


def _setting(settings: Mapping[str, str], name: str, current: str) -> str:
    value = settings.get(RUNTIME_SETTING_KEYS[name])
    return current if value is None else value


def _setting_int(settings: Mapping[str, str], name: str, current: int) -> int:
    value = settings.get(RUNTIME_SETTING_KEYS[name])
    if value is None:
        return current
    try:
        return int(value)
    except ValueError:
        return current


def _setting_float(settings: Mapping[str, str], name: str, current: float) -> float:
    value = settings.get(RUNTIME_SETTING_KEYS[name])
    if value is None:
        return current
    try:
        return float(value)
    except ValueError:
        return current


def apply_runtime_settings(config: Config, settings: Mapping[str, str]) -> Config:
    """Overlay legacy DB runtime.* keys on top of bootstrap env config."""
    if not settings:
        return config
    return with_env_variants(_overlay_legacy(config, settings))


def _overlay_legacy(config: Config, settings: Mapping[str, str]) -> Config:
    return replace(
        config,
        llm=replace(
            config.llm,
            api_key=_setting(settings, "llm_api_key", config.llm.api_key),
            api_url=_setting(settings, "llm_api_url", config.llm.api_url),
            model=_setting(settings, "llm_model", config.llm.model),
            temperature=_setting_float(settings, "llm_temperature", config.llm.temperature),
            max_tokens=_setting_int(settings, "llm_max_tokens", config.llm.max_tokens),
        ),
        embedding=replace(
            config.embedding,
            api_key=_setting(settings, "embedding_api_key", config.embedding.api_key),
            api_url=_setting(settings, "embedding_api_url", config.embedding.api_url),
            model=_setting(settings, "embedding_model", config.embedding.model),
            dimensions=_setting_int(settings, "embedding_dimensions", config.embedding.dimensions),
            rerank_api_url=_setting(settings, "rerank_api_url", config.embedding.rerank_api_url),
            rerank_model=_setting(settings, "rerank_model", config.embedding.rerank_model),
            rerank_top_n=_setting_int(settings, "rerank_top_n", config.embedding.rerank_top_n),
        ),
        typesafe=replace(
            config.typesafe,
            api_key=_setting(settings, "typesafe_api_key", config.typesafe.api_key),
            api_url=_setting(settings, "typesafe_api_url", config.typesafe.api_url),
            model=_setting(settings, "typesafe_model", config.typesafe.model),
        ),
        qdrant=replace(
            config.qdrant,
            url=_setting(settings, "qdrant_url", config.qdrant.url),
            collection_name=_setting(settings, "qdrant_collection", config.qdrant.collection_name),
        ),
        rules_path=_setting(settings, "rules_path", config.rules_path),
        workspace=_setting(settings, "workspace", config.workspace),
    )


async def load_runtime_config(pg, base_config: Config | None = None) -> Config:
    """数据库里的后台配置叠加到环境变量之上:旧 runtime.* 键 → 运行参数 system.* → 模型角色绑定。

    只读数据库,不动运行参数的进程缓存(评测脚本用它读生产库的模型配置,旋钮仍只看环境变量)。"""
    from app import model_registry, settings

    base = base_config or Config.from_env()
    legacy = await pg.get_settings(list(RUNTIME_SETTING_KEYS.values()))
    config = apply_runtime_settings(base, legacy)
    values = await settings.read(pg)
    config = replace(
        config,
        qdrant=replace(config.qdrant,
                       url=values.get("system.qdrant_url") or config.qdrant.url,
                       collection_name=values.get("system.qdrant_collection") or config.qdrant.collection_name),
        rules_path=values.get("system.rules_path", config.rules_path),
        workspace=values.get("system.workspace") or config.workspace,
    )
    registry = await model_registry.fetch(pg)
    return model_registry.apply_roles(config, registry, values)

