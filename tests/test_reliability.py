"""Durable job and PostgreSQL→Qdrant outbox behavior."""

import asyncio
import json

import pytest
from starlette.requests import Request

from app.background import execute_job
from app.config import EmbeddingConfig
from app.core import Enzyme, Pattern
from app.storage.sync import SyncManager


def _enzyme(eid: str = "e1") -> Enzyme:
    return Enzyme(id=eid, patterns=[Pattern(value="python")], then="write a test")


class FakePG:
    def __init__(self):
        self.enzyme = _enzyme()
        self.pending = []
        self.synced = []
        self.failed = []

    async def vector_sync_generation(self, enzyme_id, operation):
        return 7

    async def get_enzyme(self, enzyme_id):
        return self.enzyme if enzyme_id == self.enzyme.id else None

    async def pending_vector_sync(self, limit=100):
        return self.pending[:limit]

    async def mark_vector_synced(self, enzyme_id, operation, generation):
        self.synced.append((enzyme_id, operation, generation))

    async def mark_vector_sync_failed(self, enzyme_id, operation, generation, error):
        self.failed.append((enzyme_id, operation, generation, error))

    async def list_all_enzymes(self):
        return [self.enzyme, _enzyme("e2")]


class FakeQdrant:
    def __init__(self):
        self.embedding_config = EmbeddingConfig(
            api_key="key", api_url="http://embedding"
        )
        self.upserts = []
        self.deletes = []
        self.batches = []

    async def upsert_enzyme(self, enzyme, vector):
        self.upserts.append((enzyme.id, vector))

    async def delete_enzyme(self, enzyme_id):
        self.deletes.append(enzyme_id)

    async def delete_exemplars(self, enzyme_id):
        pass

    async def delete_lexical(self, enzyme_id):
        pass

    async def upsert_batch(self, enzymes, vectors):
        self.batches.append(([e.id for e in enzymes], list(vectors)))

    async def delete_points_not_in(self, valid_ids):
        self.valid_ids = valid_ids
        return 0


@pytest.mark.asyncio
async def test_failed_immediate_sync_stays_in_outbox(monkeypatch):
    import app.storage.sync as sync_module

    pg = FakePG()
    qdrant = FakeQdrant()

    async def fake_embed(text, config=None):
        return [1.0]

    async def fail_upsert(enzyme, vector):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(sync_module, "embed", fake_embed)
    qdrant.upsert_enzyme = fail_upsert
    await SyncManager(pg, qdrant).sync_enzyme(pg.enzyme)

    assert pg.synced == []
    assert pg.failed and pg.failed[0][:3] == ("e1", "upsert", 7)


@pytest.mark.asyncio
async def test_pending_upsert_and_delete_are_retried(monkeypatch):
    import app.storage.sync as sync_module

    pg = FakePG()
    pg.pending = [
        {"enzyme_id": "e1", "operation": "upsert", "generation": 3},
        {"enzyme_id": "gone", "operation": "delete", "generation": 4},
    ]
    qdrant = FakeQdrant()

    async def fake_embed(text, config=None):
        return [2.0]

    monkeypatch.setattr(sync_module, "embed", fake_embed)
    completed = await SyncManager(pg, qdrant).retry_pending()

    assert completed == 2
    assert qdrant.upserts == [("e1", [2.0])]
    assert qdrant.deletes == ["gone"]
    assert pg.synced == [("e1", "upsert", 3), ("gone", "delete", 4)]


@pytest.mark.asyncio
async def test_full_sync_uses_complete_authoritative_set(monkeypatch):
    import app.storage.sync as sync_module

    pg = FakePG()
    qdrant = FakeQdrant()

    async def fake_embed(text, config=None):
        return [3.0]

    monkeypatch.setattr(sync_module, "embed", fake_embed)
    synced = await SyncManager(pg, qdrant).full_sync()

    assert synced == 2
    assert qdrant.valid_ids == {"e1", "e2"}


@pytest.mark.asyncio
async def test_full_sync_retries_failed_embeddings(monkeypatch):
    """并发阶段被限流漏掉的条目要补一轮,不能静默缺向量。"""
    import app.storage.sync as sync_module

    pg = FakePG()
    qdrant = FakeQdrant()
    calls: list[str] = []

    async def flaky_embed(text, config=None):
        calls.append(text)
        return None if len(calls) == 1 else [4.0]

    async def no_sleep(_):
        return None

    monkeypatch.setattr(sync_module, "embed", flaky_embed)
    monkeypatch.setattr(sync_module.asyncio, "sleep", no_sleep)
    synced = await SyncManager(pg, qdrant).full_sync(concurrency=2)

    assert synced == 2
    assert len(calls) == 3  # 两条首轮 + 失败那条重试一次


@pytest.mark.asyncio
async def test_capture_job_dispatches_persisted_payload(monkeypatch):
    from app.learning import capture

    received = {}

    async def fake_capture(engine, transcript, **kwargs):
        received.update({"transcript": transcript, **kwargs})
        return {"created": 1}

    monkeypatch.setattr(capture, "capture_session", fake_capture)
    engine = type("Engine", (), {})()
    job = {
        "kind": "capture",
        "payload": {
            "transcript": "session text",
            "domain": "python",
            "session_id": "s1",
            "project": "demo",
        },
    }

    assert await execute_job(engine, job) == {"created": 1}
    assert received == {
        "transcript": "session text",
        "domain": "python",
        "session_id": "s1",
        "project": "demo",
    }


@pytest.mark.asyncio
async def test_job_worker_clears_capture_transcript(monkeypatch):
    import app.background as background

    finished = asyncio.Event()

    class JobPG:
        def __init__(self):
            self.claimed = False
            self.clear_payload = None

        async def claim_background_job(self, kinds):
            if self.claimed:
                return None
            self.claimed = True
            return {"id": "j1", "kind": "capture", "payload": {"transcript": "secret"}}

        async def finish_background_job(self, job_id, result, *, clear_payload=False):
            self.clear_payload = clear_payload
            finished.set()

        async def fail_background_job(self, *args, **kwargs):
            raise AssertionError("job should not fail")

    async def fake_execute(engine, job):
        return {"created": 1}

    monkeypatch.setattr(background, "execute_job", fake_execute)
    engine = type("Engine", (), {"pg": JobPG()})()
    task = asyncio.create_task(background.job_worker(engine, ["capture"]))
    try:
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert engine.pg.clear_payload is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_readyz_reports_dependency_state():
    from app.main import ready

    class ReadyPG:
        async def ping(self):
            return True

        async def schema_version(self):
            return 2

        async def vector_sync_pending_count(self):
            return 3

    class ReadyQdrant:
        async def ping(self):
            return True

    engine = type("Engine", (), {"pg": ReadyPG(), "qdrant": ReadyQdrant()})()
    mcp = type("MCP", (), {"started": True})()
    worker = type("Worker", (), {"done": lambda self: False})()
    state = type(
        "State",
        (),
        {"engine": engine, "mcp_server": mcp, "background_tasks": [worker]},
    )()
    app = type("App", (), {"state": state})()
    response = await ready(Request({"type": "http", "app": app}))

    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ready",
        "checks": {"postgresql": True, "qdrant": True, "mcp": True, "workers": True},
        "schema_version": 2,
        "vector_sync_pending": 3,
    }
