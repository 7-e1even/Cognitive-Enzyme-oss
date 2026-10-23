"""FastAPI REST API."""

import asyncio
import json
import logging
import os
import secrets as _secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()  # 确保 .env 在任何入口都能加载

from app import __version__, runtime
from app.config import Config
from app.background import job_worker, vector_sync_worker
from app.core import Pattern
from app.core.engine import Engine
from app.mcp_server import mcp, set_shared_engine
from app.routers import admin, enzymes, operations, evolution, graph, auth, gardener, systemone
from app.distill.archive import archive_loop
from app.routers.schedule import schedule, load_schedule_state
from app.security import (
    api_token_valid,
    auth_enabled,
    configure as configure_auth,
    configure_api_token,
    hash_password,
    machine_api_access_allowed,
    seed_credentials,
    token_user_active,
    verify_token,
)
from app.storage.qdrant_store import QdrantStore
from app.storage.pg_store import PGStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    base_config = Config.from_env()

    pg = PGStore(base_config.pg)
    await pg.connect()
    await pg.init_schema()

    # 旧配置迁移 → 运行参数与模型注册表载入 → 解析引擎配置(后台改动之后由 runtime.reload 热更新)
    config = await runtime.boot(pg)
    from app.llm_client import set_default_configs
    set_default_configs(config.llm, config.embedding)

    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    collection_created = await qdrant.ensure_collection()

    engine = Engine(config, pg, qdrant)
    set_shared_engine(engine)

    # 游离任务也进列表:有引用才不会被 GC 中途回收,shutdown 时也随 bg_tasks 一起取消
    early_tasks: list[asyncio.Task] = []
    if collection_created:
        queued = await pg.enqueue_all_vector_sync()
        logger.info("Qdrant collection created; queued %d enzymes for reindex", queued)
    elif qdrant.lexical_created:
        # 升级:词面索引刚建好,给已有记忆补齐(本地计算,不调 embedding;后台跑不挡启动)
        early_tasks.append(asyncio.create_task(engine.sync.backfill_lexical()))

    recovered_jobs = await pg.recover_interrupted_jobs()
    if recovered_jobs:
        logger.warning("Re-queued %d interrupted background jobs", recovered_jobs)
    last_consolidation = await pg.latest_background_job("consolidate")
    if last_consolidation and last_consolidation["status"] in ("succeeded", "failed"):
        finished_at = last_consolidation.get("finished_at")
        schedule.last_run = finished_at.isoformat() if finished_at else None
        schedule.last_result = (
            {"error": last_consolidation.get("error") or "巩固任务失败"}
            if last_consolidation["status"] == "failed"
            else last_consolidation.get("result") or None
        )

    # Auth config: persisted JWT secret (env override) + enabled flag (default ON)
    jwt_secret = os.getenv("CE_JWT_SECRET", "").strip() or await pg.get_setting("jwt_secret")
    if not jwt_secret:
        jwt_secret = _secrets.token_urlsafe(32)
        await pg.set_setting("jwt_secret", jwt_secret)
    enabled = (await pg.get_setting("auth_enabled", "true")) != "false"
    configure_auth(secret=jwt_secret, enabled=enabled)

    # MCP auth token: env seeds DB on first run; DB is the source of truth.
    from app.mcp_server import apply_mcp_auth
    env_mcp_token = os.getenv("CE_MCP_AUTH_TOKEN", "").strip()
    mcp_token = await pg.get_setting("mcp_auth_token", "") or ""
    if env_mcp_token and not mcp_token:
        await pg.set_setting("mcp_auth_token", env_mcp_token)
        mcp_token = env_mcp_token
    if not mcp_token:
        # 默认安全:MCP 暴露 report/harvest 等写工具,空令牌等于端口裸奔。
        # 首次启动自动生成并入库,在「设置 → 访问与安全」可见、可轮换。
        mcp_token = _secrets.token_urlsafe(32)
        await pg.set_setting("mcp_auth_token", mcp_token)
        logger.info("已自动生成 MCP 访问令牌(管理后台 → 设置 → 访问与安全 查看),MCP 默认要求认证")
    apply_mcp_auth(mcp_token)

    # Static REST API machine token: env seeds DB on first run; DB is source of truth.
    # Used by unattended clients (e.g. the Claude Code prime hook) — never expires.
    env_api_token = os.getenv("CE_API_TOKEN", "").strip()
    api_token = await pg.get_setting("api_auth_token", "") or ""
    if env_api_token and not api_token:
        await pg.set_setting("api_auth_token", env_api_token)
        api_token = env_api_token
    configure_api_token(api_token)

    # Restore persisted consolidation schedule (interval/enabled/DreamGlia)
    await load_schedule_state(pg)

    # Seed the initial admin user if no users exist yet (idempotent)
    await _seed_admin(pg)

    # Auto-import seeds if DB is empty (idempotent)
    count = await pg.count_enzymes()
    if count == 0:
        await _import_seeds(engine)

    # Durable, bounded workers: one capture and one maintenance job at a time.
    bg_tasks = [
        *early_tasks,
        asyncio.create_task(_consolidation_loop(engine)),
        asyncio.create_task(job_worker(engine, ["capture"])),
        asyncio.create_task(job_worker(engine, ["consolidate"])),
        asyncio.create_task(vector_sync_worker(engine)),
        asyncio.create_task(archive_loop(engine)),   # 判定数据归档(判定记录平台)
    ]

    # Start gardener nightly scheduler (服务端定时夜巡)
    from app.routers.gardener import gardener_loop
    bg_tasks.append(asyncio.create_task(gardener_loop(engine)))

    # Start MCP Server(端口 CE_MCP_PORT,默认 8421)
    import uvicorn
    from app.config import MCP_PORT
    mcp_config = uvicorn.Config(mcp.http_app(), host="0.0.0.0", port=MCP_PORT, log_level="warning")
    mcp_server = uvicorn.Server(mcp_config)
    mcp_task = asyncio.create_task(mcp_server.serve())
    await _wait_for_mcp_start(mcp_server, mcp_task)

    application.state.engine = engine
    application.state.config = config
    application.state.boot_config = config
    application.state.mcp_server = mcp_server
    application.state.background_tasks = bg_tasks
    bg_tasks.append(asyncio.create_task(runtime.watch(application)))   # 多副本:别处改了后台配置就重新载入

    logger.info("REST API started (PG + Qdrant connected, %d enzymes)", await pg.count_enzymes())
    logger.info("MCP Server started on http://0.0.0.0:%d/mcp", MCP_PORT)
    yield

    # Shutdown: cancel background tasks
    mcp_server.should_exit = True
    for task in bg_tasks:
        task.cancel()

    await asyncio.gather(mcp_task, *bg_tasks, return_exceptions=True)
    await engine.drain()

    await qdrant.close()
    await pg.close()
    set_shared_engine(None)
    logger.info("REST API and MCP Server stopped")


async def _wait_for_mcp_start(server, task: asyncio.Task) -> None:
    """Do not declare REST ready when the sibling MCP port failed to bind."""
    for _ in range(100):
        if server.started:
            return
        if task.done():
            task.result()
            raise RuntimeError("MCP server exited during startup")
        await asyncio.sleep(0.05)
    raise RuntimeError("MCP server did not become ready within 5 seconds")


async def _consolidation_loop(engine: Engine) -> None:
    """Periodically enqueue one durable consolidation job.

    按上次巩固完成的时间排下一次:重启不会立刻再跑一遍全量巩固(以前每次启动 60 秒后都会排一次)。"""
    await asyncio.sleep(60)  # Warm-up

    while True:
        wait = schedule.due_in()
        if wait > 0:
            await asyncio.sleep(min(wait, max(schedule.interval, 60)))
            continue
        if schedule.enabled and schedule.interval > 0:
            try:
                job_id = await engine.pg.enqueue_job(
                    "consolidate",
                    {"trigger": "scheduled"},
                    dedupe_key="consolidate",
                )
                if job_id:
                    logger.info("Scheduled consolidation job %s", job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Could not enqueue scheduled consolidation")

        await asyncio.sleep(max(schedule.interval, 60))  # Min 60s


async def _seed_admin(pg: PGStore) -> None:
    """Create the initial admin user on first run (idempotent).

    Auth is ON by default, so an account must always exist. Credentials come
    from CE_ADMIN_USER/PASSWORD;密码缺省时生成随机密码并只在这里打一次日志,
    避免 admin/admin 默认口令随一键脚本直接暴露到公网。
    """
    if await pg.count_users() > 0:
        return  # already have users; never auto-create another
    import uuid

    username, password = seed_credentials()
    if not password:
        password = _secrets.token_urlsafe(12)
        await pg.create_user(
            user_id=uuid.uuid4().hex,
            username=username,
            password_hash=hash_password(password),
            display_name=username,
            role="admin",
        )
        logger.warning(
            "⚠️  已创建初始管理员 %s,随机密码:%s(仅本次日志可见,请登录后立即修改;"
            "也可用 CE_ADMIN_PASSWORD 指定)", username, password,
        )
        return
    await pg.create_user(
        user_id=uuid.uuid4().hex,
        username=username,
        password_hash=hash_password(password),
        display_name=username,
        role="admin",
    )
    logger.info("Seeded initial admin user: %s", username)


async def _import_seeds(engine: Engine) -> None:
    """Import seed enzymes from data/seeds/*.json if DB is empty."""
    seeds_dir = Path(__file__).parent.parent / "data" / "seeds"
    if not seeds_dir.exists():
        return
    for seed_file in seeds_dir.glob("*.json"):
        logger.info("Auto-importing seed: %s", seed_file.name)
        data = json.loads(seed_file.read_text(encoding="utf-8"))
        for item in data:
            patterns = [Pattern(**p) for p in item.get("patterns", [])]
            if not patterns:
                continue
            await engine.create_enzyme(
                patterns=patterns,
                then_action=item.get("then", item.get("then_action", "")),
                because=item.get("because", ""),
                domain=item.get("domain", ""),
                tags=item.get("tags", []),
                references=item.get("references", []),
                severity=item.get("severity", "info"),
                confidence=item.get("confidence", 0.5),
            )
    logger.info("Seed import complete")


app = FastAPI(
    title="Cognitive Enzyme",
    description="System 1 直觉层 — REST API",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # 令牌走 Authorization 头(存 localStorage),不用 cookie,无需 credentials;
    # 关掉后浏览器跨域不再附带任何凭据,避免"反射 Origin + credentials"组合。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth gate ─────────────────────────────────────────────────
# 认证开启时(默认)保护所有 /api/* 数据端点:放行登录/状态端点、健康检查与 CORS 预检;
# 静态机器令牌只能访问数据面白名单(app/security.py)。
from starlette.responses import JSONResponse  # noqa: E402

_AUTH_PUBLIC_PATHS = {"/api/auth/login", "/api/auth/status", "/health"}


@app.middleware("http")
async def auth_gate(request, call_next):
    path = request.url.path
    if (
        not auth_enabled()
        or request.method == "OPTIONS"
        or path in _AUTH_PUBLIC_PATHS
        or path.startswith("/api/avatars/")  # 头像图片公开（<img> 无法带 token）
        or not path.startswith("/api/")
    ):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if verify_token(token):
        # 仅验签名不够:停用用户/改密后未过期的 JWT 必须失效。
        # 查库结果短缓存(security.token_user_active),不拖慢 prime 热路径。
        application = request.scope.get("app")
        engine = getattr(getattr(application, "state", None), "engine", None)
        if engine is None:
            # 引擎未就绪(测试中/启动早期):退回签名级校验
            return await call_next(request)
        if await token_user_active(token, engine.pg):
            return await call_next(request)
        return JSONResponse(status_code=401, content={"detail": "用户不存在或已被禁用"})
    if api_token_valid(token):
        if machine_api_access_allowed(request.method, path):
            return await call_next(request)
        return JSONResponse(
            status_code=403,
            content={"detail": "机器令牌无权访问管理接口"},
        )
    return JSONResponse(status_code=401, content={"detail": "未授权，请先登录"})

# ── Routers ───────────────────────────────────────────────────
from app.routers import schedule as schedule_router  # noqa: E811

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(enzymes.router)
app.include_router(operations.router)
app.include_router(evolution.router)
app.include_router(graph.router)
app.include_router(schedule_router.router)
app.include_router(gardener.router)
app.include_router(systemone.router)

# 头像静态文件（公开访问，<img> 标签直接引用）
from starlette.staticfiles import StaticFiles  # noqa: E402

app.mount("/api/avatars", StaticFiles(directory=str(auth.AVATAR_DIR)), name="avatars")


@app.get("/health")
async def health(request: Request):
    # llm/embedding 未配置时 System-2(capture/进化/向量召回)会静默 no-op,
    # 在健康检查里显式暴露,前端据此提示用户完成配置。
    config = getattr(request.app.state, "config", None)
    return {
        "status": "ok",
        "service": "cognitive-enzyme",
        "version": __version__,
        "llm_configured": bool(config and config.llm.available),
        "embedding_configured": bool(
            config and config.embedding.api_key and config.embedding.api_url
        ),
    }


@app.get("/readyz")
async def ready(request: Request):
    """Dependency-aware readiness for deployment traffic gates."""
    engine = getattr(request.app.state, "engine", None)
    checks = {"postgresql": False, "qdrant": False, "mcp": False, "workers": False}
    details: dict = {}
    if engine is not None:
        try:
            checks["postgresql"] = await engine.pg.ping()
            details["schema_version"] = await engine.pg.schema_version()
            details["vector_sync_pending"] = await engine.pg.vector_sync_pending_count()
        except Exception:
            # /readyz 是公开端点:异常原文(主机名/DSN 片段)只进日志,不对外返回
            logger.exception("readyz: PostgreSQL check failed")
            details["postgresql_error"] = "unavailable"
        try:
            checks["qdrant"] = await engine.qdrant.ping()
        except Exception:
            logger.exception("readyz: Qdrant check failed")
            details["qdrant_error"] = "unavailable"

    mcp_server = getattr(request.app.state, "mcp_server", None)
    checks["mcp"] = bool(mcp_server and mcp_server.started)
    tasks = getattr(request.app.state, "background_tasks", [])
    checks["workers"] = bool(tasks) and all(not task.done() for task in tasks)
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not_ready", "checks": checks, **details},
    )
