"""Qdrant vector storage — replaces Meilisearch for semantic search.

Handles collection management, enzyme upsert/delete, and vector search
with payload filtering (status, domain, etc.).
"""

from __future__ import annotations

import logging
import time
import uuid


from qdrant_client import AsyncQdrantClient, models

from app.config import QdrantConfig, EmbeddingConfig
from app.core import Enzyme

logger = logging.getLogger(__name__)

# Qdrant point IDs must be an unsigned int or a UUID. Production enzyme ids are
# always uuid4 (see app.core.Enzyme default_factory) so this is a no-op there,
# but other id schemes (e.g. eval corpus ids like "physics-040") need a stable
# hash. We deterministically derive a UUID5 and keep the real enzyme id in the
# payload so callers reading search results still get back the real id.
_POINT_ID_NAMESPACE = uuid.UUID("6e7d6d8a-6b8b-4a3e-9b8e-9c2f8a5f8b31")


def _is_valid_point_id(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _point_id(enzyme_id: str) -> str:
    if _is_valid_point_id(enzyme_id):
        return enzyme_id
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, enzyme_id))


def exemplar_point_id(enzyme_id: str, text: str, kind: str = "pos") -> str:
    """经验样本点 id = (酶, 正/反例, 样本原文) 的确定性 UUID5:同一条样本重复写入是幂等的。"""
    tag = "exemplar" if kind == "pos" else "anti"
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"{tag}|{enzyme_id}|{text}"))


def enzyme_text(enzyme: Enzyme) -> str:
    """Searchable text of an enzyme (dense embedding and BM25 index use the same text)."""
    return f"{enzyme.patterns_readable} {enzyme.then} {enzyme.because}"


class QdrantStore:
    """Qdrant vector store for enzyme semantic search."""

    lexical_created: bool = False

    def __init__(self, qdrant_config: QdrantConfig, embedding_config: EmbeddingConfig):
        self._config = qdrant_config
        self._emb_config = embedding_config
        self._client: AsyncQdrantClient | None = None
        self._lexical_ok = True      # 建索引失败(如 Qdrant 版本过旧不支持稀疏向量)后暂停读写
        self._lexical_retry_at = 0.0  # 瞬时故障的延迟重试时刻(monotonic),别一挂就永久禁用

    async def connect(self) -> None:
        self._client = AsyncQdrantClient(url=self._config.url)
        logger.info("Qdrant connected: %s", self._config.url)

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    @property
    def collection(self) -> str:
        return self._config.collection_name

    @property
    def exemplar_collection(self) -> str:
        """经验样本的向量索引:与酶本体分开存,一条酶可有多条样本(多向量)。"""
        return f"{self._config.collection_name}_exemplars"

    @property
    def lexical_collection(self) -> str:
        """词面(BM25 稀疏向量)索引,见 app/matching/lexical.py。"""
        return f"{self._config.collection_name}_lexical"

    @property
    def embedding_config(self) -> EmbeddingConfig:
        return self._emb_config

    def update_embedding_config(self, embedding_config: EmbeddingConfig) -> None:
        self._emb_config = embedding_config

    async def ensure_collection(self) -> bool:
        """Ensure the collection exists; return True when it was (re)created."""
        await self._ensure_exemplar_collection()
        self.lexical_created = await self._ensure_lexical_collection()
        if await self._client.collection_exists(self.collection):
            # Validate vector dimensions match config
            try:
                info = await self._client.get_collection(self.collection)
                existing_size = info.config.params.vectors.size
                if existing_size != self._emb_config.dimensions:
                    # 破坏性操作:重建会清空该 collection 的全部向量,需要 full_sync
                    # 重灌。典型误触场景是换了 embedding 模型却漏配 EMBEDDING_DIMENSIONS
                    # (回退默认 1536)——升级为 error 级并把后果说全,让部署方在日志里
                    # 一眼看到,而不是静默丢库。
                    logger.error(
                        "Qdrant dimension mismatch: collection '%s' has %d dims but "
                        "config says %d (model=%s). RECREATING — all vectors in this "
                        "collection will be DROPPED and need a full_sync re-import. "
                        "If this is unexpected, check EMBEDDING_DIMENSIONS / "
                        "app_settings embedding_dimensions against the embedding model.",
                        self.collection, existing_size, self._emb_config.dimensions,
                        self._emb_config.model,
                    )
                    await self._client.delete_collection(self.collection)
                else:
                    logger.debug("Qdrant collection '%s' already exists", self.collection)
                    return False
            except Exception as e:
                logger.debug("Qdrant dimension check failed (%s), skipping", e)
                return False

        try:
            await self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self._emb_config.dimensions,
                    distance=models.Distance.COSINE,
                ),
            )
        except Exception as e:
            # 多副本同时首启的 check-then-create 竞态:别人已建好就当已存在处理
            if await self._client.collection_exists(self.collection):
                logger.info("Qdrant collection '%s' appeared concurrently, reusing", self.collection)
                return False
            raise e
        # Create payload indices for filtering
        for field in ["domain", "status", "severity", "origin", "tags"]:
            await self._client.create_payload_index(
                collection_name=self.collection,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        await self._client.create_payload_index(
            collection_name=self.collection,
            field_name="confidence",
            field_schema=models.PayloadSchemaType.FLOAT,
        )
        logger.info("Qdrant collection '%s' created (%d dims)",
                    self.collection, self._emb_config.dimensions)
        return True

    async def _ensure_exemplar_collection(self) -> None:
        name = self.exemplar_collection
        try:
            if await self._client.collection_exists(name):
                info = await self._client.get_collection(name)
                if info.config.params.vectors.size == self._emb_config.dimensions:
                    return
                # 维度变了(换了 embedding 模型):样本向量全部作废,由 full_sync 重灌
                logger.error("Exemplar collection '%s' dimension mismatch, recreating", name)
                await self._client.delete_collection(name)
            await self._client.create_collection(
                collection_name=name,
                vectors_config=models.VectorParams(size=self._emb_config.dimensions,
                                                   distance=models.Distance.COSINE),
            )
            for field in ("enzyme_id", "domain"):
                await self._client.create_payload_index(
                    collection_name=name, field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD)
        except Exception as e:
            logger.warning("Exemplar collection setup failed (exemplar recall disabled): %s", e)

    async def _ensure_lexical_collection(self) -> bool:
        """返回 True 表示刚新建(调用方需回填已有记忆,见 SyncManager.backfill_lexical)。"""
        name = self.lexical_collection
        try:
            if await self._client.collection_exists(name):
                self._lexical_ok = True
                return False
            await self._client.create_collection(
                collection_name=name, vectors_config={},
                sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
            )
            for field in ("domain", "status"):
                await self._client.create_payload_index(
                    collection_name=name, field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD)
            self._lexical_ok = True
            return True
        except Exception as e:
            logger.warning("Lexical collection setup failed (lexical recall disabled): %s", e)
            self._lexical_ok = False
            return False

    _LEXICAL_RETRY_INTERVAL = 60.0  # 词面索引故障后的重试间隔(秒)

    async def _lexical_available(self) -> bool:
        """词面索引当前是否可用;启动时的瞬时故障每 60s 惰性重试一次,
        不再一次抖动就永久禁用(旧实现 _lexical_ok 只能在启动路径复位)。"""
        if self._lexical_ok:
            return True
        now = time.monotonic()
        if now < self._lexical_retry_at:
            return False
        self._lexical_retry_at = now + self._LEXICAL_RETRY_INTERVAL
        await self._ensure_lexical_collection()  # 成功时内部把 _lexical_ok 置回 True
        return self._lexical_ok

    async def upsert_lexical(self, enzymes: list[Enzyme]) -> None:
        from app.matching.lexical import doc_vector
        if not await self._lexical_available():
            return
        points = []
        for e in enzymes:
            vec = doc_vector(enzyme_text(e))
            if vec:
                points.append(models.PointStruct(
                    id=_point_id(e.id),
                    vector={"bm25": models.SparseVector(indices=vec[0], values=vec[1])},
                    payload={"enzyme_id": e.id, "domain": e.domain, "status": e.status}))
        if points:
            await self._client.upsert(collection_name=self.lexical_collection, points=points)

    async def delete_lexical(self, enzyme_id: str) -> None:
        if not await self._lexical_available():
            return
        await self._client.delete(collection_name=self.lexical_collection,
                                  points_selector=models.PointIdsList(points=[_point_id(enzyme_id)]))

    async def search_lexical(self, text: str, domain: str | None = None, limit: int = 10) -> list[dict]:
        """BM25 检索:返回 [{id: 酶 id, _score}](分数与余弦不可比,由调用方按名次合并)。"""
        from app.matching.lexical import query_vector
        qv = query_vector(text) if await self._lexical_available() else None
        if not qv:
            return []
        must = [models.FieldCondition(key="domain", match=models.MatchValue(value=domain))] if domain else None
        must_not = [models.FieldCondition(key="status", match=models.MatchAny(any=["quarantine", "rejected"]))]
        try:
            results = await self._client.query_points(
                collection_name=self.lexical_collection,
                query=models.SparseVector(indices=qv[0], values=qv[1]), using="bm25",
                query_filter=models.Filter(must=must, must_not=must_not), limit=limit)
        except Exception as e:
            logger.debug("Lexical search failed: %s", e)
            return []
        return [{"id": (p.payload or {}).get("enzyme_id"), "_score": p.score} for p in results.points]

    async def exemplar_point_ids(self, enzyme_id: str) -> set[str]:
        """该酶当前已入索引的样本点 id(增量同步用)。"""
        ids: set[str] = set()
        offset = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=self.exemplar_collection, limit=256, offset=offset,
                scroll_filter=models.Filter(must=[models.FieldCondition(
                    key="enzyme_id", match=models.MatchValue(value=enzyme_id))]),
                with_payload=False, with_vectors=False)
            ids |= {str(p.id) for p in points}
            if offset is None:
                return ids

    async def upsert_exemplars(self, enzyme: Enzyme, items: list[tuple[str, list[float]]],
                               kind: str = "pos") -> None:
        if not items:
            return
        await self._client.upsert(
            collection_name=self.exemplar_collection,
            points=[models.PointStruct(id=exemplar_point_id(enzyme.id, text, kind), vector=vec,
                                       payload={"enzyme_id": enzyme.id, "domain": enzyme.domain,
                                                "text": text, "kind": kind})
                    for text, vec in items])

    async def delete_exemplar_points(self, point_ids: list[str]) -> None:
        if point_ids:
            await self._client.delete(collection_name=self.exemplar_collection,
                                      points_selector=models.PointIdsList(points=point_ids))

    async def delete_exemplars(self, enzyme_id: str) -> None:
        await self._client.delete(
            collection_name=self.exemplar_collection,
            points_selector=models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(
                key="enzyme_id", match=models.MatchValue(value=enzyme_id))])))

    async def search_exemplars(self, vector: list[float], domain: str | None = None,
                               limit: int = 30) -> list[dict]:
        """按情境检索经验样本:返回 [{id: 酶 id, _score, text}](同一酶可能多条,由调用方合并)。"""
        query_filter = None
        if domain:
            query_filter = models.Filter(must=[models.FieldCondition(
                key="domain", match=models.MatchValue(value=domain))])
        try:
            results = await self._client.query_points(
                collection_name=self.exemplar_collection, query=vector,
                query_filter=query_filter, limit=limit)
        except Exception as e:
            logger.debug("Exemplar search failed: %s", e)
            return []
        return [{"id": (p.payload or {}).get("enzyme_id"), "_score": p.score,
                 "text": (p.payload or {}).get("text", ""), "kind": (p.payload or {}).get("kind", "pos")}
                for p in results.points]

    async def ping(self) -> bool:
        return bool(await self._client.collection_exists(self.collection))

    async def delete_points_not_in(self, valid_enzyme_ids: set[str]) -> int:
        """Remove legacy/orphaned points left behind by failed deletes."""
        stale_point_ids = []
        offset = None
        while True:
            points, offset = await self._client.scroll(
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=["enzyme_id"],
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                enzyme_id = payload.get("enzyme_id") or str(point.id)
                if str(enzyme_id) not in valid_enzyme_ids:
                    stale_point_ids.append(point.id)
            if offset is None:
                break
        if stale_point_ids:
            await self._client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(points=stale_point_ids),
            )
        return len(stale_point_ids)

    async def upsert_enzyme(self, enzyme: Enzyme, vector: list[float]) -> None:
        """Upsert a single enzyme with its embedding vector."""
        await self._client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=_point_id(enzyme.id),
                    vector=vector,
                    payload=_enzyme_to_payload(enzyme),
                )
            ],
        )

    async def upsert_batch(
        self, enzymes: list[Enzyme], vectors: list[list[float]]
    ) -> None:
        """Batch upsert enzymes with their vectors."""
        if not enzymes or not vectors:
            return
        points = [
            models.PointStruct(
                id=_point_id(e.id),
                vector=v,
                payload=_enzyme_to_payload(e),
            )
            for e, v in zip(enzymes, vectors)
        ]
        await self._client.upsert(
            collection_name=self.collection,
            points=points,
        )

    async def delete_enzyme(self, enzyme_id: str) -> None:
        """Delete a single enzyme by ID."""
        await self._client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[_point_id(enzyme_id)]),
        )

    async def get_vectors(self, enzyme_ids: list[str]) -> dict[str, list[float]]:
        """批量取向量(酶 id → vector)。dedup 等场景复用索引里已有向量,
        不必为每条记忆再调一次付费 embedding API。"""
        if not enzyme_ids:
            return {}
        points = await self._client.retrieve(
            collection_name=self.collection,
            ids=[_point_id(eid) for eid in enzyme_ids],
            with_payload=False,
            with_vectors=True,
        )
        out: dict[str, list[float]] = {}
        # point id 是酶 id 的确定性映射(uuid5),反查回酶 id
        pid_to_eid = {_point_id(eid): eid for eid in enzyme_ids}
        for point in points:
            eid = pid_to_eid.get(str(point.id))
            vector = getattr(point, "vector", None)
            if eid and isinstance(vector, list):
                out[eid] = vector
        return out

    async def search(
        self,
        vector: list[float],
        domain: str | None = None,
        limit: int = 50,
        include_all: bool = False,
    ) -> list[dict]:
        """Vector search with payload filtering.

        By default excludes quarantine/rejected enzymes.
        Set include_all=True for dedup checks that need to see everything.
        """
        # Build filter conditions
        must_conditions = []
        must_not_conditions = []

        if not include_all:
            must_not_conditions.append(
                models.FieldCondition(
                    key="status",
                    match=models.MatchValue(value="quarantine"),
                )
            )
            must_not_conditions.append(
                models.FieldCondition(
                    key="status",
                    match=models.MatchValue(value="rejected"),
                )
            )

        if domain:
            must_conditions.append(
                models.FieldCondition(
                    key="domain",
                    match=models.MatchValue(value=domain),
                )
            )

        query_filter = None
        if must_conditions or must_not_conditions:
            query_filter = models.Filter(
                must=must_conditions or None,
                must_not=must_not_conditions or None,
            )

        try:
            results = await self._client.query_points(
                collection_name=self.collection,
                query=vector,
                query_filter=query_filter,
                limit=limit,
            )
            # Convert to dicts compatible with existing code
            hits = []
            for point in results.points:
                hit = dict(point.payload) if point.payload else {}
                # payload carries the real enzyme id (point.id may be a
                # UUID5 hash for non-UUID enzyme ids — see _point_id)
                hit["id"] = hit.pop("enzyme_id", None) or point.id
                hit["_score"] = point.score
                hits.append(hit)
            return hits
        except Exception as e:
            logger.warning("Qdrant search failed: %s", e)
            return []

    async def get_stats(self) -> dict:
        try:
            info = await self._client.get_collection(self.collection)
            return {"documents": info.points_count or 0}
        except Exception:
            return {"documents": 0}


# ── Helpers ────────────────────────────────────────────────────

def _enzyme_to_payload(enzyme: Enzyme) -> dict:
    """Convert Enzyme to Qdrant payload (metadata for filtering)."""
    return {
        "enzyme_id": enzyme.id,
        "patterns_text": enzyme.patterns_readable,
        "then": enzyme.then,
        "because": enzyme.because,
        "severity": enzyme.severity.value,
        "references": enzyme.references,
        "confidence": enzyme.confidence,
        "precision": enzyme.precision,
        "trigger_count": enzyme.trigger_count,
        "origin": enzyme.origin.value,
        "domain": enzyme.domain,
        "tags": enzyme.tags,
        "status": enzyme.status,
        "quality_score": enzyme.quality_score,
        "updated_at": enzyme.updated_at.isoformat() if enzyme.updated_at else "",
    }
