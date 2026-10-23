"""运行时配置的载入与热更新:运行参数 + 模型注册表 → 引擎配置。

后台改动(本进程的管理接口,或其他副本——由 :func:`watch` 发现)都走 :func:`reload`,
一次性刷新参数缓存、模型注册表、引擎配置与 embedding 默认值,无需重启。只有向量库连接与
向量维度的变化要重启才生效(:func:`restart_required` 对比启动时的配置)。
"""

from __future__ import annotations

import asyncio
import logging

from app import model_registry, settings
from app.config import Config, load_runtime_config

logger = logging.getLogger(__name__)


async def boot(pg) -> Config:
    """服务启动:迁移旧配置 → 载入运行参数与模型注册表 → 解析引擎配置。"""
    try:
        migrated = await model_registry.migrate_legacy(pg)
        if migrated:
            logger.info("Legacy runtime config migrated into the admin registry (%d items)", len(migrated))
    except Exception:
        logger.exception("Legacy runtime config migration failed (old values stay in effect)")
    await settings.load(pg)
    await model_registry.load(pg)
    return await load_runtime_config(pg, Config.from_env())


async def load_admin_state(pg) -> None:
    """离线工具(app/distill 的 CLI)连上库之后调用:与服务进程用同一份运行参数与模型注册表。"""
    await settings.load(pg)
    await model_registry.load(pg)


async def cli_admin_state(timeout: float = 5.0) -> bool:
    """CLI 尽力而为地载入后台配置(连不上库就只用环境变量,离线训练机也能跑)。"""
    from app.storage.pg_store import PGStore

    pg = PGStore(Config.from_env().pg)
    try:
        await asyncio.wait_for(pg.connect(), timeout)
        await load_admin_state(pg)
        return True
    except Exception as e:
        logger.info("Admin config not loaded (%s); using environment variables only", type(e).__name__)
        return False
    finally:
        try:
            await pg.close()
        except Exception:
            pass


async def reload(app) -> Config:
    """后台改动后热更新引擎配置(新请求立即使用)。"""
    from app.llm_client import set_default_configs

    engine = app.state.engine
    await settings.load(engine.pg)
    await model_registry.load(engine.pg)
    config = await load_runtime_config(engine.pg, Config.from_env())
    set_default_configs(config.llm, config.embedding)
    engine.config = config
    engine.qdrant.update_embedding_config(config.embedding)
    app.state.config = config
    return config


def restart_required(app) -> list[str]:
    """与启动时相比需要重启才生效的改动(向量库连接、集合名、向量维度)。"""
    boot_config: Config | None = getattr(app.state, "boot_config", None)
    config: Config | None = getattr(app.state, "config", None)
    if boot_config is None or config is None:
        return []
    out = []
    if boot_config.qdrant.url != config.qdrant.url:
        out.append("system.qdrant_url")
    if boot_config.qdrant.collection_name != config.qdrant.collection_name:
        out.append("system.qdrant_collection")
    if boot_config.embedding.dimensions != config.embedding.dimensions:
        out.append("model.embedding")
    return out


async def watch(app, interval: float = 15.0) -> None:
    """多副本对齐:运行参数或模型注册表在别处被改,就重新载入。"""
    pg = app.state.engine.pg
    # 首次取指纹也放进循环保护里:启动瞬间 PG 抖一下不该永久杀死 watch 任务
    last = None
    while True:
        await asyncio.sleep(interval)
        try:
            fp = await pg.admin_fingerprint()
            if last is not None and fp != last:
                await reload(app)
                logger.info("Runtime config reloaded (changed by another process)")
            last = fp
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("runtime config watch failed (will retry)", exc_info=True)
