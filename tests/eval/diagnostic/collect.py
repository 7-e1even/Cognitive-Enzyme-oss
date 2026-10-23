"""Collect complete retrieval evidence in an isolated server process.

Only model configuration is read from PostgreSQL. All matching reads frozen
files; new vector collections have a mandatory ce_diag_ prefix. No Engine.match,
learning, outcomes, schema initialization, or production writes are used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, append, canonical, dump, rows, sha, verify

CURRENT = contextvars.ContextVar("diagnostic_record", default=None)


class RecordingHTTP:
    def __init__(self, directory: Path, secrets: list[str]):
        import httpx
        self.client = httpx.AsyncClient(timeout=90)
        self.directory, self.secrets = directory, [s for s in secrets if s]
        self.calls, self.hits = defaultdict(int), defaultdict(int)

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    async def post(self, url, *, headers=None, json=None, timeout=None):
        import httpx
        kind = "embedding" if url.rstrip("/").endswith("embeddings") else "rerank" if url.rstrip("/").endswith("rerank") else "jev"
        key = sha(canonical({"endpoint_sha256": sha(url), "payload": json}))
        path = self.directory / kind / (key + ".json")
        cached = __import__("json").loads(path.read_text()) if path.exists() else None
        if cached and cached.get("status") == 200:
            self.hits[kind] += 1
            saved = cached
        else:
            start = time.perf_counter()
            try:
                response = await self.client.post(url, headers=headers, json=json, timeout=timeout or 90)
                try:
                    body = response.json()
                except ValueError:
                    body = {"body": response.text[:2000]}
                saved = {"kind": kind, "request": json, "status": response.status_code,
                         "response": self.clean(body), "secs": round(time.perf_counter() - start, 3)}
            except httpx.HTTPError as exc:
                saved = {"kind": kind, "request": json, "status": 599,
                         "response": {"error": type(exc).__name__},
                         "secs": round(time.perf_counter() - start, 3)}
            self.calls[kind] += 1
            # Keep failed attempts as well; a successful later attempt is not allowed to erase them.
            if path.exists():
                append(path.with_suffix(".attempts.jsonl"), saved)
            dump(path, saved)
        rec = CURRENT.get()
        if rec is not None:
            rec.setdefault("api_refs", []).append({"kind": kind, "key": key, "status": saved["status"]})
        return httpx.Response(saved["status"], json=saved["response"], request=httpx.Request("POST", url))


async def configuration():
    import asyncpg
    from app import model_registry, settings
    from app.config import Config, load_runtime_config
    from app.storage.pg_store import PGStore

    base = Config.from_env()
    pg = PGStore(base.pg)
    # Enforce read-only even if a future config helper accidentally writes.
    pg._pool = await asyncpg.create_pool(base.pg.dsn, min_size=1, max_size=2,
                                        server_settings={"default_transaction_read_only": "on"})
    try:
        config = await load_runtime_config(pg, base)
        values = await settings.read(pg)
        registry = await model_registry.fetch(pg)
    finally:
        await pg.close()
    bindings = dict(values)
    defaults = {"model.embedding": "bge-m3", "model.rerank": "bge-reranker-v2-m3", "model.judge": "jev-1.13"}
    for key, model in defaults.items():
        if not bindings.get(key) and registry.resolve(model):
            bindings[key] = model
    config = model_registry.apply_roles(config, registry, bindings)
    settings._values.clear()
    settings._values.update(values)
    # Controlled collection does not add speculative memories or extra chat models.
    overrides = {"ranking.exploration_rate": 0.0, "judge.llm": False,
                 "judge.llm_fallback": False, "recall.query_expand": False}
    settings._values.update(overrides)
    policy = {k: settings.get(k) for k in settings.SPECS
              if k.startswith(("recall.", "ranking.", "judge.", "selection.", "exemplar."))}
    return config, registry, {"models": {"embedding": config.embedding.model,
                                         "embedding_dimensions": config.embedding.dimensions,
                                         "rerank": config.embedding.rerank_model,
                                         "jev": config.typesafe.model},
                              "policy": policy, "overrides": overrides,
                              "role_bindings": {k: bindings.get(k, "") for k in defaults}}


class FrozenPG:
    def __init__(self, bank: list[dict], excluded: set[str]):
        from app.core import Enzyme
        self.memories = {}
        for row in bank:
            if row["id"] in excluded:
                continue
            value = {**row, "precision": row.get("precision_", .5)}
            self.memories[row["id"]] = Enzyme.model_validate(value)

    async def get_enzymes(self, ids):
        return [self.memories[i] for i in ids if i in self.memories]

    async def get_related_batch(self, ids, limit=5):
        return {}

    async def routable_domains(self):
        return [{"name": d, "description": ""} for d in sorted({e.domain for e in self.memories.values() if e.domain})]

    async def keyword_search(self, words, domain=None, limit=50):
        matches = [(sum(w.lower() in (e.patterns_readable + e.then).lower() for w in words), e)
                   for e in self.memories.values() if not domain or e.domain == domain]
        return [e for score, e in sorted(matches, key=lambda x: -x[0]) if score > 0][:limit]


class FilteredVectors:
    def __init__(self, store, excluded, indexed, rec):
        self.store, self.excluded, self.indexed, self.rec = store, excluded, indexed, rec

    def filt(self, domain=None, exemplar=False):
        from qdrant_client import models as m
        must = [m.FieldCondition(key="domain", match=m.MatchValue(value=domain))] if domain else []
        blocked = [m.FieldCondition(key="enzyme_id", match=m.MatchAny(any=sorted(self.excluded)))] if self.excluded else []
        if not exemplar:
            must.append(m.FieldCondition(key="status", match=m.MatchValue(value="active")))
        return m.Filter(must=must or None, must_not=blocked or None)

    async def search(self, vector, domain=None, limit=50, **kw):
        from qdrant_client import models as m
        if not vector:
            return []
        client = self.store._client
        args = dict(collection_name=self.store.collection, query=vector,
                    query_filter=self.filt(domain), with_payload=True)
        approx = await client.query_points(**args, limit=limit)
        exact = await client.query_points(**args, limit=max(1, len(self.indexed)),
                                          search_params=m.SearchParams(exact=True))
        def hit(point):
            return {**(point.payload or {}), "id": (point.payload or {}).get("enzyme_id") or str(point.id), "_score": point.score}
        values = [hit(p) for p in approx.points]
        self.rec.setdefault("dense_searches", []).append({
            "domain": domain, "limit": limit, "indexed_ids": sorted(self.indexed - self.excluded),
            "hits": [[h["id"], h["_score"]] for h in values],
            "exact_ranking": [[hit(p)["id"], p.score] for p in exact.points]})
        return values

    async def search_lexical(self, text, domain=None, limit=10):
        from app.matching.lexical import query_vector
        from qdrant_client import models as m
        vector = query_vector(text)
        if not vector:
            return []
        result = await self.store._client.query_points(
            collection_name=self.store.lexical_collection,
            query=m.SparseVector(indices=vector[0], values=vector[1]), using="bm25",
            query_filter=self.filt(domain), limit=limit)
        values = [{"id": (p.payload or {}).get("enzyme_id"), "_score": p.score} for p in result.points]
        self.rec["lexical_hits"] = values
        return values

    async def search_exemplars(self, vector, domain=None, limit=30):
        result = await self.store._client.query_points(
            collection_name=self.store.exemplar_collection, query=vector,
            query_filter=self.filt(domain, exemplar=True), limit=limit)
        values = [{"id": (p.payload or {}).get("enzyme_id"), "_score": p.score,
                   "text": (p.payload or {}).get("text", ""),
                   "kind": (p.payload or {}).get("kind", "pos")} for p in result.points]
        self.rec["exemplar_hits"] = [{"id": h["id"], "score": h["_score"], "kind": h["kind"],
                                      "text_sha256": sha(h["text"])} for h in values]
        return values


async def embeddings(directory, tasks, banks, config, http):
    from app.core.features import build_search_query
    from app.storage.qdrant_store import enzyme_text
    texts = {}
    def add(text):
        text = text[:8000]
        if text:
            texts[sha(text)] = text
    for bank in banks.values():
        for e in FrozenPG(bank, set()).memories.values():
            add(enzyme_text(e))
            for text in e.exemplars + e.anti_exemplars:
                add(text)
    for task in tasks:
        add(build_search_query(task["problem"]["problem"]))
    path = directory / "embeddings.jsonl"
    vectors = {r["sha256"]: r["vector"] for r in rows(path)}
    pending = [key for key in texts if key not in vectors]
    print(f"embedding cached={len(vectors)} pending={len(pending)}", flush=True)
    # Serial batches avoid monopolizing a shared provider's rate limit.
    for offset in range(0, len(pending), 16):
        keys = pending[offset:offset + 16]
        payload = {"model": config.embedding.model, "input": [texts[k] for k in keys]}
        response = await http.post(config.embedding.api_url.rstrip("/") + "/embeddings",
                                   headers={"Authorization": "Bearer " + config.embedding.api_key}, json=payload)
        response.raise_for_status()
        data = response.json().get("data", [])
        if len(data) != len(keys):
            raise ValueError("Incomplete embedding response")
        for item in data:
            key = keys[item["index"]]
            vector = item["embedding"]
            if len(vector) != config.embedding.dimensions:
                raise ValueError("Embedding dimension mismatch")
            vectors[key] = vector
            append(path, {"sha256": key, "input": texts[key], "vector": vector})
        if offset % 160 == 0:
            print(f"embedding {len(vectors)}/{len(texts)}", flush=True)
    return vectors


async def index(directory, banks, vectors, config, fingerprint):
    from app.storage.qdrant_store import QdrantStore, enzyme_text
    stores = {}
    for group, bank in banks.items():
        name = f"ce_diag_{fingerprint[:10]}_{group}"
        if not name.startswith("ce_diag_"):
            raise ValueError("Unsafe collection name")
        store = QdrantStore(replace(config.qdrant, collection_name=name), config.embedding)
        await store.connect()
        marker = directory / f"index-{group}.json"
        if not marker.exists():
            await store.ensure_collection()
            enzymes = list(FrozenPG(bank, set()).memories.values())
            for offset in range(0, len(enzymes), 32):
                chunk = enzymes[offset:offset + 32]
                await store.upsert_batch(chunk, [vectors[sha(enzyme_text(e)[:8000])] for e in chunk])
                await store.upsert_lexical(chunk)
            for e in enzymes:
                for kind, texts in (("pos", e.exemplars), ("anti", e.anti_exemplars)):
                    await store.upsert_exemplars(e, [(t, vectors[sha(t[:8000])]) for t in texts if t.strip()], kind=kind)
            dump(marker, {"collection": name, "memories": len(enzymes), "input_sha256": fingerprint})
        stores[group] = store
        print(f"index {group} {name}", flush=True)
    return stores


async def run(args):
    import app.llm_client as llm
    import app.matching.pipeline as pipeline
    import app.matching.rerank as rr_module
    import app.matching.route as route
    import app.typesafe_client as tc
    from app import settings
    from app.core.features import build_search_query
    from app.matching.rerank import RerankResult

    directory = args.directory.resolve()
    verify(directory / "inputs")
    fingerprint = sha((directory / "inputs/manifest.json").read_bytes())
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    banks = json.loads((directory / "inputs/banks.json").read_text())
    config, registry, metadata = await configuration()
    metadata["input_sha256"] = fingerprint
    metadata["max_results"] = 6
    meta_path = directory / "retrieval-manifest.json"
    if meta_path.exists() and json.loads(meta_path.read_text()) != metadata:
        raise ValueError("Retrieval configuration changed; use another run directory")
    dump(meta_path, metadata)
    if not config.typesafe.available or not config.embedding.api_key:
        raise ValueError("Embedding/Jev credentials unavailable")
    http = RecordingHTTP(directory / "api", [p.api_key for p in registry.providers.values()])
    tc._client = http
    rr_module._client = http
    vectors = await embeddings(directory, tasks, banks, config, http)
    if args.stage == "embed":
        await http.client.aclose()
        return
    stores = await index(directory, banks, vectors, config, fingerprint)
    async def cached_embed(text, config=None):
        return vectors.get(sha(text[:8000]))
    llm.embed = cached_embed
    real_rerank = pipeline.rerank
    async def full_rerank(query, documents, config, top_n=None):
        full = await real_rerank(query, documents, config, top_n=len(documents))
        rec = CURRENT.get()
        if rec is not None:
            ids = rec["trace"].get("refined_ids", [])
            rec["rerank_full"] = [[ids[r.index], r.score] for r in (full or []) if r.index < len(ids)]
            rec["rerank_top_n"] = top_n
        return full[:top_n] if full is not None and top_n else full
    pipeline.rerank = full_rerank
    start_judges = pipeline._start_judges
    def capture_pool(context, pool, *a, **kw):
        rec = CURRENT.get()
        if rec is not None:
            rec["trace"]["judge_pool_before_fit"] = [e.id for e in pool]
        return start_judges(context, pool, *a, **kw)
    pipeline._start_judges = capture_pool
    completed = {r["id"] for r in rows(directory / "retrieval.jsonl")}
    semaphore = asyncio.Semaphore(args.concurrency)
    count = len(completed)
    selected = [t for t in tasks if t["id"] not in completed]
    if args.limit:
        selected = selected[:args.limit]
    async def one(task):
        nonlocal count
        async with semaphore:
            start = time.perf_counter()
            rec = {"id": task["id"], "group": task["group"], "expected_ids": task["expected_ids"],
                   "excluded_ids": sorted(task["excluded"]), "trace": {}, "injected_ids": []}
            token = CURRENT.set(rec)
            try:
                pg = FrozenPG(banks[task["group"]], set(task["excluded"]))
                store = FilteredVectors(stores[task["group"]], set(task["excluded"]), set(pg.memories), rec)
                # Route cache is shared per process; scope it to the frozen bank.
                # Collection is serial by default to preserve this invariant.
                route._cache = None
                result = await pipeline.run_pipeline(
                    context=task["problem"]["problem"], search_query=build_search_query(task["problem"]["problem"]),
                    pg=pg, qdrant=store, max_results=6, embedding_config=config.embedding,
                    typesafe_config=config.typesafe, trace=rec["trace"])
                picked = [r for r in result if r.enzyme.confidence >= .25 or r.exploratory][:6]
                rec["injected_ids"] = [r.enzyme.id for r in picked]
                rec["selected"] = [{"id": r.enzyme.id, "version": r.enzyme.version,
                                    "confidence": r.enzyme.confidence, "exploratory": r.exploratory,
                                    "reason": r.reason, "p": r.applicability} for r in result]
                rec["rule_input_truncated_ids"] = [eid for eid, *_ in rec["trace"].get("jev", [])
                                                    if len(pg.memories[eid].then) > tc._RULE_THEN_MAX]
                rec["state_truncated"] = len(task["problem"]["problem"]) > tc._STATE_MAX
            except Exception as exc:
                rec["error"] = type(exc).__name__ + ": " + str(http.clean(str(exc)))[:400]
            finally:
                CURRENT.reset(token)
            rec["secs"] = round(time.perf_counter() - start, 3)
            append(directory / "retrieval.jsonl", rec)
            count += 1
            if count % 10 == 0 or rec.get("error"):
                print(f"retrieval {count}/{len(tasks)} {task['id']} inject={len(rec['injected_ids'])} status={rec['trace'].get('jev_status')} error={rec.get('error', '')}", flush=True)
            if count % 20 == 0:
                dump(directory / "progress.json", {"phase": "retrieval", "complete": count,
                                                   "planned": len(tasks), "api_calls": dict(http.calls)})
    if args.concurrency != 1:
        raise ValueError("Use concurrency=1: route cache is bank-scoped")
    try:
        await asyncio.gather(*(one(t) for t in selected))
    finally:
        for store in stores.values():
            await store.close()
        await http.client.aclose()
    dump(directory / "collection-usage.json", {"calls": dict(http.calls), "cache_hits": dict(http.hits)})
    from tests.eval.diagnostic.protocol import retrieval_report
    print(json.dumps(retrieval_report(directory), ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    parser.add_argument("--stage", choices=("embed", "all"), default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
