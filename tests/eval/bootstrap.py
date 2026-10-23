# tests/eval/bootstrap.py
"""评测环境装载:独立 PG 库 + 独立 Qdrant collection,配置沿用生产 app_settings。

安全约束:本模块是评测代码唯一的建连入口,生产库只用于读 runtime 配置。
"""
from __future__ import annotations

import os
from dataclasses import replace

import asyncpg
from dotenv import load_dotenv

from app.config import Config, load_runtime_config
from app.llm_client import set_default_configs
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore

# 可用 env 覆盖:并行跑多组会改库的实验(如 RSI 闭环模拟)时各用一套库,互不污染
EVAL_DB = os.environ.get("CE_EVAL_DB", "cognitive_enzyme_eval")
EVAL_COLLECTION = os.environ.get("CE_EVAL_COLLECTION", "enzymes_eval")


async def _ensure_eval_db(base: Config) -> None:
    conn = await asyncpg.connect(dsn=base.pg.dsn, timeout=10)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", EVAL_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{EVAL_DB}"')
    finally:
        await conn.close()


async def eval_stores() -> tuple[Config, PGStore, QdrantStore]:
    load_dotenv()
    base = Config.from_env()
    assert base.pg.database != EVAL_DB, "bootstrap 期望 .env 指向生产库以读取配置"

    # 1. 从生产 PG 的 app_settings 拿 runtime 配置(embedding/rerank/llm key)
    prod_pg = PGStore(base.pg)
    await prod_pg.connect()
    config = await load_runtime_config(prod_pg, base)
    await prod_pg.close()

    # 2. 建 eval 库(幂等)
    await _ensure_eval_db(base)

    # 3. 改指向 eval 库/collection
    config = replace(
        config,
        pg=replace(config.pg, database=EVAL_DB),
        qdrant=replace(config.qdrant, collection_name=EVAL_COLLECTION),
    )
    assert config.embedding.api_key, "app_settings 缺 embedding key,无法评测"

    set_default_configs(config.llm, config.embedding)

    pg = PGStore(config.pg)
    await pg.connect()
    await pg.init_schema()
    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    await qdrant.ensure_collection()
    if qdrant.lexical_created:
        from app.storage.sync import SyncManager
        await SyncManager(pg, qdrant).backfill_lexical()
    return config, pg, qdrant


if __name__ == "__main__":  # 冒烟: python -m tests.eval.bootstrap
    import asyncio

    async def _smoke():
        config, pg, qdrant = await eval_stores()
        n = await pg.count_enzymes()
        print(f"eval PG={config.pg.database} enzymes={n} "
              f"qdrant={config.qdrant.collection_name} "
              f"embed_model={config.embedding.model}")
        await pg.close()

    asyncio.run(_smoke())
