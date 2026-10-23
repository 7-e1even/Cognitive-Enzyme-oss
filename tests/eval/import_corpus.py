# tests/eval/import_corpus.py
"""语料 → eval PG + Qdrant。绕过 harvest 质量门(评测语料是已知资产)。

用法: python -m tests.eval.import_corpus [--wipe] [--concurrency N]
--wipe 先清空 eval 库(TRUNCATE enzymes + 删除重建 collection)。
"""
from __future__ import annotations

import asyncio
import sys

from app.storage.sync import SyncManager
from tests.eval.bootstrap import eval_stores
from tests.eval.validate import CORPUS_DIR, load_corpus


async def main() -> None:
    config, pg, qdrant = await eval_stores()
    if "--wipe" in sys.argv:
        # 注意: PGStore/QdrantStore 只暴露 _pool/_client(无公开 pool/client
        # 属性),brief 里的 pg.pool/qdrant.client 需改成实际私有属性名。
        async with pg._pool.acquire() as conn:
            await conn.execute("TRUNCATE enzymes, enzyme_relations, evolution_log CASCADE")
        for name in (config.qdrant.collection_name, qdrant.exemplar_collection, qdrant.lexical_collection):
            try:
                await qdrant._client.delete_collection(name)
            except Exception:
                pass
        await qdrant.connect()  # 重建 collection
        await qdrant.ensure_collection()
        print("已清空 eval 库")

    enzymes = load_corpus(CORPUS_DIR)
    for e in enzymes:
        await pg.upsert_enzyme(e)
    print(f"PG 导入 {len(enzymes)} 条")

    conc = int(sys.argv[sys.argv.index("--concurrency") + 1]) if "--concurrency" in sys.argv else 1
    synced = await SyncManager(pg, qdrant).full_sync(concurrency=conc)
    print(f"Qdrant 同步 {synced} 条")
    assert synced == len(enzymes), f"同步缺失: {synced}/{len(enzymes)}"
    await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
