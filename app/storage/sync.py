"""Sync — one-way PostgreSQL → Qdrant synchronization."""

from __future__ import annotations

import asyncio
import logging

from app.core import Enzyme
from app.llm_client import embed
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore, enzyme_text, exemplar_point_id

logger = logging.getLogger(__name__)

BATCH_SIZE = 20  # Batch size for full_sync upserts


class SyncManager:
    """Keeps Qdrant in sync with PostgreSQL (PG is source of truth)."""

    def __init__(self, pg: PGStore, qdrant: QdrantStore):
        self.pg = pg
        self.qdrant = qdrant

    async def sync_enzyme(self, enzyme: Enzyme, generation: int | None = None) -> None:
        """Try the durable PG outbox item immediately.

        ``generation`` 应传 upsert_enzyme 的返回值(写入事务里自增的那一代);
        缺省时回退到事后读取——与并发写入存在竞态窗口,只留给不方便改的旧调用点。
        """
        if generation is None:
            generation = await self.pg.vector_sync_generation(enzyme.id, "upsert")
        if generation is None:
            return
        cfg = self.qdrant.embedding_config
        if not cfg.api_key or not cfg.api_url:
            return  # durable outbox keeps it pending until configuration exists
        try:
            text = _enzyme_text(enzyme)
            vector = await embed(text, config=cfg)
            if not vector:
                raise RuntimeError("embedding unavailable")
            await self.qdrant.upsert_enzyme(enzyme, vector)
            await self.pg.mark_vector_synced(enzyme.id, "upsert", generation)
            await self.sync_exemplars(enzyme)
            await self.sync_lexical([enzyme])
        except Exception as e:
            await self.pg.mark_vector_sync_failed(
                enzyme.id, "upsert", generation, str(e)
            )
            logger.warning("Qdrant sync deferred for %s: %s", enzyme.id, e)

    async def sync_exemplars(self, enzyme: Enzyme) -> None:
        """经验样本增量同步:只给新增样本算向量,删掉已移出的样本点。

        样本索引是派生数据(PG exemplars 为准),失败不影响主流程,下次同步自动补齐。"""
        try:
            existing = await self.qdrant.exemplar_point_ids(enzyme.id)
            want: dict[str, tuple[str, str]] = {}
            for kind, texts in (("pos", enzyme.exemplars), ("anti", enzyme.anti_exemplars)):
                for t in texts:
                    want[exemplar_point_id(enzyme.id, t, kind)] = (kind, t)
            for kind in ("pos", "anti"):
                items = []
                for pid, (k, text) in want.items():
                    if k != kind or pid in existing:
                        continue
                    vector = await embed(text, config=self.qdrant.embedding_config)
                    if vector:
                        items.append((text, vector))
                await self.qdrant.upsert_exemplars(enzyme, items, kind=kind)
            await self.qdrant.delete_exemplar_points([pid for pid in existing if pid not in want])
        except Exception as e:
            logger.warning("Exemplar sync deferred for %s: %s", enzyme.id, e)

    async def sync_lexical(self, enzymes: list[Enzyme]) -> None:
        """词面索引(派生数据,本地计算、不调 API):失败不影响主流程,下次同步/回填补齐。"""
        try:
            await self.qdrant.upsert_lexical(enzymes)
        except Exception as e:
            logger.warning("Lexical sync deferred (%d enzymes): %s", len(enzymes), e)

    async def backfill_lexical(self) -> int:
        """词面索引刚建好时,给库里已有的记忆一次性补齐(无需 embedding)。"""
        enzymes = await self.pg.list_all_enzymes()
        for i in range(0, len(enzymes), 256):
            await self.sync_lexical(enzymes[i:i + 256])
        logger.info("Lexical index backfilled: %d enzymes", len(enzymes))
        return len(enzymes)

    async def _delete_all_indexes(self, enzyme_id: str) -> None:
        """主 collection + 派生索引(样本/词面)一起删——缺了后者会留孤儿点,
        它们不在 full_sync 的 delete_points_not_in 清理范围内,永远不消失。"""
        await self.qdrant.delete_enzyme(enzyme_id)
        for derived in (self.qdrant.delete_exemplars, self.qdrant.delete_lexical):
            try:
                await derived(enzyme_id)
            except Exception as e:
                logger.warning("Derived index delete deferred for %s: %s", enzyme_id, e)

    async def sync_delete(self, enzyme_id: str) -> None:
        """After PG delete, remove from Qdrant."""
        generation = await self.pg.vector_sync_generation(enzyme_id, "delete")
        if generation is None:
            return
        try:
            await self._delete_all_indexes(enzyme_id)
            await self.pg.mark_vector_synced(enzyme_id, "delete", generation)
        except Exception as e:
            await self.pg.mark_vector_sync_failed(
                enzyme_id, "delete", generation, str(e)
            )
            logger.warning("Qdrant delete deferred for %s: %s", enzyme_id, e)

    async def retry_pending(self, limit: int = 100) -> int:
        """Retry persisted outbox entries; safe across restarts."""
        pending = await self.pg.pending_vector_sync(limit=limit)
        completed = 0
        for item in pending:
            enzyme_id = item["enzyme_id"]
            operation = item["operation"]
            generation = item["generation"]
            try:
                if operation == "delete":
                    await self._delete_all_indexes(enzyme_id)
                else:
                    cfg = self.qdrant.embedding_config
                    if not cfg.api_key or not cfg.api_url:
                        continue
                    enzyme = await self.pg.get_enzyme(enzyme_id)
                    if enzyme is None:
                        await self._delete_all_indexes(enzyme_id)
                    else:
                        vector = await embed(
                            _enzyme_text(enzyme),
                            config=cfg,
                        )
                        if not vector:
                            raise RuntimeError("embedding unavailable")
                        await self.qdrant.upsert_enzyme(enzyme, vector)
                        await self.sync_exemplars(enzyme)
                        await self.sync_lexical([enzyme])
                await self.pg.mark_vector_synced(enzyme_id, operation, generation)
                completed += 1
            except Exception as e:
                await self.pg.mark_vector_sync_failed(
                    enzyme_id, operation, generation, str(e)
                )
                logger.warning("Vector sync retry failed for %s: %s", enzyme_id, e)
        return completed

    async def full_sync(self, concurrency: int = 1) -> int:
        """Full re-sync: read all from PG, push to Qdrant in batches.

        ``concurrency`` > 1 并发调用 embedding API(换模型重建索引时用;远端 API 的限流决定上限),
        默认 1 与原先逐条串行一致。

        开始前快照 outbox 代际:成功同步的条目把快照代际的清掉(期间又有新写入的
        留下给 retry_pending),否则每次全量同步后整个队列都会被重放一遍 embedding。"""
        queue_snapshot = {
            item["enzyme_id"]: item["generation"]
            for item in await self.pg.pending_vector_sync(limit=100000)
        }
        enzymes = await self.pg.list_all_enzymes()
        synced = 0
        sem = asyncio.Semaphore(max(1, concurrency))

        async def vector_of(enzyme: Enzyme) -> list[float] | None:
            async with sem:
                return await embed(_enzyme_text(enzyme), config=self.qdrant.embedding_config)

        async def upsert(pairs: list[tuple[Enzyme, list[float]]]) -> int:
            for j in range(0, len(pairs), BATCH_SIZE):
                part = pairs[j:j + BATCH_SIZE]
                await self.qdrant.upsert_batch([e for e, _ in part], [v for _, v in part])
            return len(pairs)

        failed: list[Enzyme] = []
        ok_ids: set[str] = set()
        step = BATCH_SIZE * max(1, concurrency)
        for i in range(0, len(enzymes), step):
            chunk = enzymes[i:i + step]
            vectors = await asyncio.gather(*(vector_of(e) for e in chunk))
            failed += [e for e, v in zip(chunk, vectors) if not v]
            ok_ids.update(e.id for e, v in zip(chunk, vectors) if v)
            synced += await upsert([(e, v) for e, v in zip(chunk, vectors) if v])
        if failed:
            # 限流/瞬时故障漏掉的条目放慢逐条补一轮:并发重建索引时不因个别失败静默缺向量
            await asyncio.sleep(5)
            retried = []
            for e in failed:
                v = await embed(_enzyme_text(e), config=self.qdrant.embedding_config)
                if v:
                    retried.append((e, v))
            synced += await upsert(retried)
            ok_ids.update(e.id for e, _ in retried)
            if len(retried) < len(failed):
                logger.warning("Full sync: %d enzymes still without vectors after retry",
                               len(failed) - len(retried))

        removed = await self.qdrant.delete_points_not_in({e.id for e in enzymes})
        for i in range(0, len(enzymes), 256):
            await self.sync_lexical(enzymes[i:i + 256])
        # 清掉快照代际的 upsert 队列行:数据已是最新;embed 失败的与期间有新写入的
        # (代际更大)保留,留给 retry_pending 重放
        for enzyme_id, generation in queue_snapshot.items():
            if enzyme_id in ok_ids:
                await self.pg.mark_vector_synced(enzyme_id, "upsert", generation)
        for enzyme in enzymes:
            if enzyme.exemplars or enzyme.anti_exemplars:
                await self.sync_exemplars(enzyme)

        logger.info(
            "Full sync: %d/%d enzymes synced, %d stale points removed",
            synced,
            len(enzymes),
            removed,
        )
        return synced


_enzyme_text = enzyme_text
