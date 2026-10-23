"""Consolidation router — 调度配置 + 手动触发 + 最近一次任务状态(/api/consolidation)。"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.engine import Engine
from app.dependencies import get_engine
from app.routers._helpers import audit
from app.routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["schedule"])

_SETTING_KEY = "schedule_state"


class ScheduleState:
    """Shared state for the background consolidation scheduler."""

    def __init__(self, interval: int = 6 * 3600, enabled: bool = True):
        self.interval = interval  # seconds
        self.enabled = enabled
        self.running = False
        self.last_run: str | None = None
        self.last_result: dict | None = None
        # DreamGlia (dream engine + immune patrol)
        self.dreamglia_enabled: bool = True
        self.dreamglia_max_dreams: int = 2
        # 实验性进化机制(LLM 驱动的重组引擎),默认关
        self.mutation_enabled: bool = False
        self.crossover_enabled: bool = False
        # 情景回放测试(离线验证,给探索槽假设与低反馈酶判卷),默认开
        self.replay_enabled: bool = True
        self.replay_max_episodes: int = 5

    def due_in(self, now: datetime | None = None) -> float:
        """距下一次定时巩固还有多少秒(≤ 0 = 该跑了):以上次巩固完成的时间为准,没跑过就是现在。"""
        if not self.last_run:
            return 0.0
        try:
            last = datetime.fromisoformat(self.last_run)
        except ValueError:
            return 0.0
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return self.interval - ((now or datetime.now(timezone.utc)) - last).total_seconds()

    def persistable(self) -> dict:
        return {
            "interval": self.interval,
            "enabled": self.enabled,
            "dreamglia_enabled": self.dreamglia_enabled,
            "dreamglia_max_dreams": self.dreamglia_max_dreams,
            "mutation_enabled": self.mutation_enabled,
            "crossover_enabled": self.crossover_enabled,
            "replay_enabled": self.replay_enabled,
            "replay_max_episodes": self.replay_max_episodes,
        }


# Global schedule state — shared between API and background task
schedule = ScheduleState()


async def load_schedule_state(pg) -> None:
    """Restore persisted schedule settings from app_settings (startup)."""
    raw = await pg.get_setting(_SETTING_KEY, "")
    if not raw:
        return
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring corrupt %s setting", _SETTING_KEY)
        return
    if "interval" in data:
        schedule.interval = max(60, int(data["interval"]))
    if "enabled" in data:
        schedule.enabled = bool(data["enabled"])
    if "dreamglia_enabled" in data:
        schedule.dreamglia_enabled = bool(data["dreamglia_enabled"])
    if "dreamglia_max_dreams" in data:
        schedule.dreamglia_max_dreams = max(1, min(10, int(data["dreamglia_max_dreams"])))
    if "mutation_enabled" in data:
        schedule.mutation_enabled = bool(data["mutation_enabled"])
    if "crossover_enabled" in data:
        schedule.crossover_enabled = bool(data["crossover_enabled"])
    if "replay_enabled" in data:
        schedule.replay_enabled = bool(data["replay_enabled"])
    if "replay_max_episodes" in data:
        schedule.replay_max_episodes = max(1, min(20, int(data["replay_max_episodes"])))


def _schedule_public() -> dict:
    return {
        "interval": schedule.interval,
        "enabled": schedule.enabled,
        "dreamglia_enabled": schedule.dreamglia_enabled,
        "dreamglia_max_dreams": schedule.dreamglia_max_dreams,
        "mutation_enabled": schedule.mutation_enabled,
        "crossover_enabled": schedule.crossover_enabled,
        "replay_enabled": schedule.replay_enabled,
        "replay_max_episodes": schedule.replay_max_episodes,
    }


async def _job_public(engine: Engine) -> dict:
    job = await engine.pg.latest_background_job("consolidate")
    if job is None:
        return {"running": False, "status": None, "job_id": None, "last_run": None, "last_result": None}
    result = job.get("result") or None
    if job["status"] == "failed":
        result = {"error": job.get("error") or "巩固任务失败"}
    return {
        "running": job["status"] in ("pending", "running"),
        "status": job["status"],
        "job_id": job["id"],
        "last_run": job["finished_at"].isoformat() if job.get("finished_at") else None,
        "last_result": result,
    }


class ScheduleUpdate(BaseModel):
    interval: int | None = Field(None, ge=60)
    enabled: bool | None = None
    dreamglia_enabled: bool | None = None
    dreamglia_max_dreams: int | None = Field(None, ge=1, le=10)
    mutation_enabled: bool | None = None
    crossover_enabled: bool | None = None
    replay_enabled: bool | None = None
    replay_max_episodes: int | None = Field(None, ge=1, le=20)


@router.get("/consolidation")
async def get_consolidation(engine: Engine = Depends(get_engine)):
    """巩固调度配置 + 最近一次巩固任务的状态与结果。"""
    return {"schedule": _schedule_public(), "job": await _job_public(engine)}


@router.put("/consolidation/schedule")
async def update_schedule(body: ScheduleUpdate, engine: Engine = Depends(get_engine),
                          user: dict = Depends(get_current_user)):
    """更新巩固调度(持久化,重启后保留)。"""
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(schedule, key, value)
    await engine.pg.set_setting(_SETTING_KEY, json.dumps(schedule.persistable()))
    await audit(engine, user, "consolidation.schedule", "", changes)
    return {"schedule": _schedule_public(), "job": await _job_public(engine)}


@router.post("/consolidation/run")
async def run_consolidation(engine: Engine = Depends(get_engine), user: dict = Depends(get_current_user)):
    """立即排队一次巩固(同一时刻只有一个)。"""
    job_id = await engine.pg.enqueue_job("consolidate", {"trigger": "manual"}, dedupe_key="consolidate")
    if job_id is None:
        raise HTTPException(status_code=409, detail="巩固任务正在运行中")
    await audit(engine, user, "consolidation.run", job_id)
    return {"schedule": _schedule_public(), "job": await _job_public(engine)}
