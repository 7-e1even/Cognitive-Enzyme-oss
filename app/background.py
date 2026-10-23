"""Bounded background workers backed by PostgreSQL jobs/outbox tables."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.core.engine import Engine

logger = logging.getLogger(__name__)


async def job_worker(engine: Engine, kinds: list[str]) -> None:
    """Run one job at a time for the supplied queue partition."""
    while True:
        try:
            job = await engine.pg.claim_background_job(kinds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not claim background job for %s", kinds)
            await asyncio.sleep(5)
            continue
        if job is None:
            await asyncio.sleep(1)
            continue

        clear_payload = job["kind"] == "capture"
        try:
            result = await execute_job(engine, job)
            await engine.pg.finish_background_job(
                job["id"], result, clear_payload=clear_payload
            )
        except asyncio.CancelledError:
            raise  # the running row is re-queued on the next startup
        except Exception as exc:
            logger.exception("Background job %s (%s) failed", job["id"], job["kind"])
            if job["kind"] == "consolidate":
                from app.routers.schedule import schedule

                schedule.last_result = {"error": str(exc)}
            try:
                await engine.pg.fail_background_job(
                    job["id"], str(exc), clear_payload=clear_payload
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # 记账本身失败(多半 PG 也在抖):记日志后回到轮询,
                # 不能让唯一的长驻 worker 就这么死掉(任务会在重启时重排队)
                logger.exception("Could not record failure for job %s", job["id"])
                await asyncio.sleep(5)


async def execute_job(engine: Engine, job: dict) -> dict:
    payload = job.get("payload") or {}
    if job["kind"] == "capture":
        from app.learning.capture import capture_session

        return await capture_session(
            engine,
            str(payload.get("transcript") or ""),
            domain=str(payload.get("domain") or ""),
            session_id=str(payload.get("session_id") or ""),
            project=str(payload.get("project") or ""),
        )

    if job["kind"] == "consolidate":
        from app.learning.consolidation import full_consolidation
        from app.routers.schedule import schedule

        schedule.running = True
        schedule.last_result = None
        try:
            result = await full_consolidation(
                pg=engine.pg,
                qdrant=engine.qdrant,
                rules_path=engine.config.rules_path,
                config=engine.config,
            )
            schedule.last_result = result
            schedule.last_run = datetime.now(timezone.utc).isoformat()
            return result
        finally:
            schedule.running = False

    raise ValueError(f"unknown background job kind: {job['kind']}")


async def vector_sync_worker(engine: Engine) -> None:
    """Continuously drain the durable PG→Qdrant outbox."""
    while True:
        try:
            completed = await engine.sync.retry_pending(limit=50)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Vector sync worker failed")
            completed = 0
        await asyncio.sleep(1 if completed else 15)
