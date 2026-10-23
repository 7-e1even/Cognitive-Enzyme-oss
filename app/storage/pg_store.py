"""PostgreSQL storage — asyncpg-based CRUD + relations + evolution log."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from app.config import PGConfig
from app.core import (
    Enzyme,
    EnzymeRelation,
    Episode,
    EvolutionEvent,
    Pattern,
)


class ConcurrencyConflict(Exception):
    """乐观锁冲突:update 时库里的 version 已不再是读取时的版本(有人抢先写入)。"""

# ── SQL ────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS enzymes (
    id                   TEXT PRIMARY KEY,
    type                 TEXT NOT NULL DEFAULT 'reflex',
    version              INT DEFAULT 1,
    patterns             JSONB NOT NULL,
    conditions           JSONB DEFAULT '[]',
    negations            JSONB DEFAULT '[]',
    then_action          TEXT NOT NULL,
    because              TEXT DEFAULT '',
    severity             TEXT DEFAULT 'info',
    references_          JSONB DEFAULT '[]',
    confidence           REAL DEFAULT 0.5,
    precision_           REAL DEFAULT 0.5,
    status               TEXT DEFAULT 'active',
    quality_score        REAL DEFAULT 0.0,
    trigger_count        INT DEFAULT 0,
    success_count        INT DEFAULT 0,
    false_positive_count INT DEFAULT 0,
    last_triggered       TIMESTAMPTZ,
    activation_count     INT DEFAULT 0,
    last_activated       TIMESTAMPTZ,
    origin               TEXT DEFAULT 'manual',
    parent_ids           TEXT[] DEFAULT '{}',
    domain               TEXT DEFAULT '',
    tags                 TEXT[] DEFAULT '{}',
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS enzyme_relations (
    source_id   TEXT REFERENCES enzymes(id) ON DELETE CASCADE,
    target_id   TEXT REFERENCES enzymes(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    PRIMARY KEY (source_id, target_id, relation)
);

CREATE TABLE IF NOT EXISTS evolution_log (
    id          SERIAL PRIMARY KEY,
    enzyme_id   TEXT REFERENCES enzymes(id) ON DELETE SET NULL,
    event_type  TEXT NOT NULL,
    detail      JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT PRIMARY KEY,
    workspace         TEXT DEFAULT '',
    features          JSONB DEFAULT '{}',
    enzymes_injected  TEXT[] DEFAULT '{}',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name  TEXT DEFAULT '',
    avatar_url    TEXT DEFAULT '',
    role          TEXT DEFAULT 'user',
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_username     ON users(username);
CREATE INDEX IF NOT EXISTS idx_enzymes_domain     ON enzymes(domain);
CREATE INDEX IF NOT EXISTS idx_enzymes_confidence ON enzymes(confidence DESC);
CREATE INDEX IF NOT EXISTS idx_enzymes_tags       ON enzymes USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_relations_source   ON enzyme_relations(source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target   ON enzyme_relations(target_id);
CREATE INDEX IF NOT EXISTS idx_log_enzyme         ON evolution_log(enzyme_id);
CREATE INDEX IF NOT EXISTS idx_sessions_time      ON sessions(created_at DESC);

-- 被动闭环:每会话对注入酶的效果评估记录(幂等去重 + 信号质量分析)
CREATE TABLE IF NOT EXISTS capture_evals (
    session_id TEXT NOT NULL,
    enzyme_id  TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (session_id, enzyme_id)
);

-- 注入不应期:prime 每轮注入的 (会话,酶) 记录,窗口内抑制重复注入。
-- 与 capture_evals 分开:这张表是短时状态(常态几百行,过期即删),那张是评估终局。
CREATE TABLE IF NOT EXISTS prime_injections (
    session_id  TEXT NOT NULL,
    enzyme_id   TEXT NOT NULL,
    injected_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (session_id, enzyme_id)
);

CREATE INDEX IF NOT EXISTS idx_prime_injections_time ON prime_injections(injected_at);

-- 情景记忆(冷层):一次会话的结构化一幕,cues 是模式补全回想的倒排键
CREATE TABLE IF NOT EXISTS episodes (
    id               TEXT PRIMARY KEY,
    session_id       TEXT DEFAULT '',
    occurred_at      TIMESTAMPTZ DEFAULT NOW(),
    domain           TEXT DEFAULT '',
    title            TEXT NOT NULL,
    intent           TEXT DEFAULT '',
    events           JSONB DEFAULT '[]',
    root_cause       TEXT DEFAULT '',
    outcome          TEXT DEFAULT '',
    lesson           TEXT DEFAULT '',
    entities         TEXT[] DEFAULT '{}',
    error_signatures TEXT[] DEFAULT '{}',
    cues             TEXT[] DEFAULT '{}',
    excerpt          TEXT DEFAULT '',
    enzyme_ids       TEXT[] DEFAULT '{}',
    salience         REAL DEFAULT 0.5,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_episodes_cues    ON episodes USING GIN(cues);
CREATE INDEX IF NOT EXISTS idx_episodes_enzymes ON episodes USING GIN(enzyme_ids);
CREATE INDEX IF NOT EXISTS idx_episodes_time    ON episodes(occurred_at DESC);

-- 回放判卷:用历史情景考现有酶的离线验证终局(判过即终局,不重判)
CREATE TABLE IF NOT EXISTS replay_evals (
    episode_id TEXT NOT NULL,
    enzyme_id  TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (episode_id, enzyme_id)
);
"""

# Run separately — asyncpg can't mix DDL + PL/pgSQL in one execute()
MIGRATION_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='enzymes' AND column_name='status') THEN
        ALTER TABLE enzymes ADD COLUMN status TEXT DEFAULT 'active';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='enzymes' AND column_name='quality_score') THEN
        ALTER TABLE enzymes ADD COLUMN quality_score REAL DEFAULT 0.0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='enzymes' AND column_name='activation_count') THEN
        ALTER TABLE enzymes ADD COLUMN activation_count INT DEFAULT 0;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='enzymes' AND column_name='last_activated') THEN
        ALTER TABLE enzymes ADD COLUMN last_activated TIMESTAMPTZ;
    END IF;
END
$$;
"""

# Indexes that depend on migrated columns — run after migration
STATUS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_enzymes_status ON enzymes(status);
CREATE INDEX IF NOT EXISTS idx_enzymes_last_activated ON enzymes(last_activated);
"""

BOOTSTRAP_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

RELIABILITY_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS vector_sync_queue (
    enzyme_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
    generation BIGINT NOT NULL DEFAULT 1,
    attempts  INT NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vector_sync_pending
    ON vector_sync_queue(updated_at, attempts);

CREATE TABLE IF NOT EXISTS background_jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    dedupe_key  TEXT,
    attempts    INT NOT NULL DEFAULT 0,
    result      JSONB NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_background_jobs_claim
    ON background_jobs(status, kind, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_background_jobs_active_dedupe
    ON background_jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');

INSERT INTO vector_sync_queue(enzyme_id, operation)
SELECT id, 'upsert' FROM enzymes
ON CONFLICT (enzyme_id) DO NOTHING;
"""

# 经验样本(Enzyme.exemplars):该记忆被证实适用过的真实情境原文,向量另建索引
EXEMPLARS_MIGRATION_SQL = """
ALTER TABLE enzymes ADD COLUMN IF NOT EXISTS exemplars JSONB DEFAULT '[]';
ALTER TABLE enzymes ADD COLUMN IF NOT EXISTS anti_exemplars JSONB DEFAULT '[]';
"""

# 会话内 prime 情境原文(短期保留):capture 事后发现某条记忆"本该被想起"时,从中挑出它真正
# 适用的那一轮情境记为经验样本——与之后 prime 的查询同分布,比 LLM 概括的触发描述更好认
PRIME_CONTEXTS_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS prime_contexts (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    context     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prime_contexts_session ON prime_contexts(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_prime_contexts_time ON prime_contexts(created_at);
"""

# 判定记录(判定记录平台,见 app/distill/records.py):每次 prime 的完整输入与决定 + 事后结果 + 老师标注
DECISIONS_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id           TEXT PRIMARY KEY,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    session_id   TEXT DEFAULT '',
    project      TEXT DEFAULT '',
    domain       TEXT DEFAULT '',
    context_hash TEXT NOT NULL,
    policy       JSONB NOT NULL,
    request      JSONB,
    answers      JSONB,
    candidates   JSONB NOT NULL,
    injected     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_log_time ON decision_log(created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_session ON decision_log(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_decision_log_ctx ON decision_log(context_hash);
CREATE TABLE IF NOT EXISTS decision_outcomes (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    decision_id  TEXT DEFAULT '',
    session_id   TEXT DEFAULT '',
    enzyme_id    TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    source       TEXT DEFAULT '',
    context_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_decision ON decision_outcomes(decision_id);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_session ON decision_outcomes(session_id, enzyme_id);
CREATE INDEX IF NOT EXISTS idx_decision_outcomes_time ON decision_outcomes(created_at);
CREATE TABLE IF NOT EXISTS teacher_labels (
    decision_id  TEXT NOT NULL,
    teacher      TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    answers      JSONB NOT NULL,
    usage        JSONB DEFAULT '{}',
    PRIMARY KEY (decision_id, teacher)
);
"""

# 后台管理(见 app/model_registry.py、app/routers/admin.py):模型服务商 / 模型注册表 + 配置审计日志
ADMIN_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS model_providers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    protocol    TEXT NOT NULL CHECK (protocol IN ('openai', 'anthropic', 'system_one')),
    base_url    TEXT NOT NULL DEFAULT '',
    api_key     TEXT NOT NULL DEFAULT '',
    headers     JSONB NOT NULL DEFAULT '{}',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS model_entries (
    id          TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES model_providers(id) ON UPDATE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('chat', 'embedding', 'rerank', 'judge')),
    model       TEXT NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_model_entries_provider ON model_entries(provider_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    detail      JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_time ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action, created_at DESC);
"""

# 语义去重的判定缓存(见 app/learning/dedup.py):两条记忆的内容、判官模型与题面都没变,
# 就沿用上次的判定概率,巩固时不再把几万对候选重新判一遍。
DEDUP_VERDICTS_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS dedup_verdicts (
    pair_key    TEXT PRIMARY KEY,
    input_hash  TEXT NOT NULL,
    probability DOUBLE PRECISION NOT NULL,
    judged_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# 领域表(见 app/learning/domains.py):新记忆入库时自动归类的候选领域与一句话描述。
# source:existing = 记忆里原有的领域(导入 / 旧数据),created = 归类时由语言模型新建。
DOMAINS_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS domains (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

SCHEMA_MIGRATIONS = (
    (1, (MIGRATION_SQL, STATUS_INDEX_SQL)),
    (2, (RELIABILITY_MIGRATION_SQL,)),
    (3, (EXEMPLARS_MIGRATION_SQL,)),
    (4, (PRIME_CONTEXTS_MIGRATION_SQL,)),
    (5, (DECISIONS_MIGRATION_SQL,)),
    (6, (ADMIN_MIGRATION_SQL,)),
    (7, (DEDUP_VERDICTS_MIGRATION_SQL,)),
    (8, (DOMAINS_MIGRATION_SQL,)),
)

_MIGRATION_LOCK_ID = 0x43454E5A  # "CENZ" — one migrator across app replicas

# 首页"帮上忙了吗"的口径(见 PGStore.home_summary):只认智能体的主反馈
_FEEDBACK_SOURCES = ("report", "report_batch", "passive")
_HELPED_OUTCOMES = ("success", "helped")
_HARMED_OUTCOMES = ("failure", "contradicted")


# ── Store ──────────────────────────────────────────────────────

class PGStore:
    """PostgreSQL storage backend."""

    def __init__(self, config: PGConfig):
        self._config = config
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._config.dsn, min_size=2, max_size=10)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(BOOTSTRAP_MIGRATION_SQL)
            await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
            try:
                await conn.execute(SCHEMA_SQL)
                rows = await conn.fetch("SELECT version FROM schema_migrations")
                applied = {row["version"] for row in rows}
                for version, statements in SCHEMA_MIGRATIONS:
                    if version in applied:
                        continue
                    async with conn.transaction():
                        for sql in statements:
                            await conn.execute(sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations(version) VALUES($1)",
                            version,
                        )
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)

    async def ping(self) -> bool:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT 1") == 1

    async def schema_version(self) -> int:
        async with self._pool.acquire() as conn:
            return int(
                await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            )

    # ── Durable vector-sync outbox ──────────────────────────

    async def pending_vector_sync(self, limit: int = 100) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT enzyme_id, operation, generation, attempts, last_error, updated_at
                FROM vector_sync_queue
                -- 失败退避:第 n 次重试至少间隔 n×15s(封顶 20 次=5 分钟一档),
                -- 中毒条目(embedding 永久拒绝)不再每 15s 烧一次付费调用,恢复后仍能自愈
                WHERE updated_at + LEAST(attempts, 20) * INTERVAL '15 seconds' <= NOW()
                ORDER BY updated_at, enzyme_id
                LIMIT $1
                """,
                limit,
            )
        return [dict(row) for row in rows]

    async def vector_sync_generation(self, enzyme_id: str, operation: str) -> int | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT generation FROM vector_sync_queue
                WHERE enzyme_id=$1 AND operation=$2
                """,
                enzyme_id,
                operation,
            )
        return int(value) if value is not None else None

    async def mark_vector_synced(
        self, enzyme_id: str, operation: str, generation: int
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM vector_sync_queue
                WHERE enzyme_id=$1 AND operation=$2 AND generation=$3
                """,
                enzyme_id,
                operation,
                generation,
            )

    async def mark_vector_sync_failed(
        self, enzyme_id: str, operation: str, generation: int, error: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vector_sync_queue
                SET attempts=attempts+1, last_error=$4, updated_at=NOW()
                WHERE enzyme_id=$1 AND operation=$2 AND generation=$3
                """,
                enzyme_id,
                operation,
                generation,
                error[:2000],
            )

    async def vector_sync_pending_count(self) -> int:
        async with self._pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM vector_sync_queue"))

    async def enqueue_all_vector_sync(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO vector_sync_queue(enzyme_id, operation, attempts, last_error, updated_at)
                SELECT id, 'upsert', 0, '', NOW() FROM enzymes
                ON CONFLICT (enzyme_id) DO UPDATE SET
                    operation='upsert',
                    generation=vector_sync_queue.generation+1,
                    attempts=0, last_error='', updated_at=NOW()
                """
            )
        return int(result.rsplit(" ", 1)[-1])

    # ── Durable bounded background jobs ─────────────────────

    async def enqueue_job(
        self, kind: str, payload: dict, dedupe_key: str | None = None
    ) -> str | None:
        job_id = uuid.uuid4().hex
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO background_jobs(id, kind, payload, dedupe_key)
                    VALUES($1, $2, $3::jsonb, $4)
                    """,
                    job_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    dedupe_key,
                )
        except asyncpg.UniqueViolationError:
            return None
        return job_id

    async def recover_interrupted_jobs(self) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE background_jobs
                SET status='pending', error='服务重启后重新排队', updated_at=NOW()
                WHERE status='running'
                """
            )
        return int(result.rsplit(" ", 1)[-1])

    async def claim_background_job(self, kinds: list[str]) -> dict | None:
        if not kinds:
            return None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH next_job AS (
                    SELECT id
                    FROM background_jobs
                    WHERE status='pending' AND kind = ANY($1::text[])
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE background_jobs AS job
                SET status='running', attempts=attempts+1, error='', updated_at=NOW()
                FROM next_job
                WHERE job.id=next_job.id
                RETURNING job.*
                """,
                kinds,
            )
        return _job_row_to_dict(row) if row else None

    async def finish_background_job(
        self, job_id: str, result: dict, *, clear_payload: bool = False
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE background_jobs
                SET status='succeeded', result=$2::jsonb, error='',
                    payload=CASE WHEN $3 THEN '{}'::jsonb ELSE payload END,
                    updated_at=NOW(), finished_at=NOW()
                WHERE id=$1
                """,
                job_id,
                json.dumps(result, ensure_ascii=False),
                clear_payload,
            )

    async def fail_background_job(
        self, job_id: str, error: str, *, clear_payload: bool = False
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE background_jobs
                SET status='failed', error=$2,
                    payload=CASE WHEN $3 THEN '{}'::jsonb ELSE payload END,
                    updated_at=NOW(), finished_at=NOW()
                WHERE id=$1
                """,
                job_id,
                error[:4000],
                clear_payload,
            )

    async def latest_background_job(self, kind: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM background_jobs
                WHERE kind=$1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                kind,
            )
        return _job_row_to_dict(row) if row else None

    # ── Capture passive-eval bookkeeping ─────────────────────

    async def filter_unevaluated(self, session_id: str, enzyme_ids: list[str]) -> list[str]:
        """返回本会话尚未得出确定评估结论的酶 id(保持入参顺序)。

        neutral 不算终局:PreCompact 时"注入了但还没被用到"的酶记为 neutral,
        SessionEnd 再评时仍需放行,才有机会升级成 helped/contradicted。
        """
        if not enzyme_ids:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT enzyme_id FROM capture_evals"
                " WHERE session_id=$1 AND enzyme_id = ANY($2::text[])"
                " AND verdict <> 'neutral'",
                session_id, enzyme_ids,
            )
        done = {r["enzyme_id"] for r in rows}
        return [e for e in enzyme_ids if e not in done]

    async def record_eval(self, session_id: str, enzyme_id: str, verdict: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO capture_evals (session_id, enzyme_id, verdict)
                VALUES ($1, $2, $3)
                ON CONFLICT (session_id, enzyme_id) DO UPDATE
                    SET verdict = EXCLUDED.verdict, created_at = NOW()
                    WHERE capture_evals.verdict = 'neutral'
                """,
                session_id, enzyme_id, verdict,
            )

    # ── 回放判卷(离线验证) ───────────────────────────────────

    async def replay_candidates(
        self, limit: int = 5, min_salience: float = 0.5,
    ) -> list[Episode]:
        """待回放的情景考题池:新近、够重要的优先。

        同一幕反复入池没有代价放大——(episode, enzyme) 终局由 replay_evals
        主键保证,重考只会考到"这幕存后新出现的酶"。
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM episodes WHERE salience >= $1"
                " ORDER BY occurred_at DESC LIMIT $2",
                min_salience, limit,
            )
        return [_row_to_episode(r) for r in rows]

    async def filter_unreplayed(self, episode_id: str, enzyme_ids: list[str]) -> list[str]:
        """返回该幕尚未判过卷的酶 id(保持入参顺序)。

        与 capture_evals 不同,回放判决即终局——情景是冻结的,neutral
        重判也不会有新信息,所以任何已记录的判决都拦截。
        """
        if not enzyme_ids:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT enzyme_id FROM replay_evals"
                " WHERE episode_id=$1 AND enzyme_id = ANY($2::text[])",
                episode_id, enzyme_ids,
            )
        done = {r["enzyme_id"] for r in rows}
        return [e for e in enzyme_ids if e not in done]

    async def record_replay_eval(self, episode_id: str, enzyme_id: str, verdict: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO replay_evals (episode_id, enzyme_id, verdict)
                VALUES ($1, $2, $3)
                ON CONFLICT (episode_id, enzyme_id) DO NOTHING
                """,
                episode_id, enzyme_id, verdict,
            )

    # ── 注入不应期 ────────────────────────────────────────────

    async def recent_injections(
        self, session_id: str, enzyme_ids: list[str], window_seconds: float,
    ) -> set[str]:
        """窗口期内已注入过的酶 id 子集(prime 热路径,一次批量查询)。"""
        if not session_id or not enzyme_ids:
            return set()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT enzyme_id FROM prime_injections"
                " WHERE session_id=$1 AND enzyme_id = ANY($2::text[])"
                " AND injected_at > NOW() - make_interval(secs => $3)",
                session_id, enzyme_ids, float(window_seconds),
            )
        return {r["enzyme_id"] for r in rows}

    async def record_injections(self, session_id: str, enzyme_ids: list[str]) -> None:
        """记录本轮实际注入,顺手清掉隔天的过期行(表常年保持很小)。"""
        if not session_id or not enzyme_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO prime_injections (session_id, enzyme_id)"
                " SELECT $1, unnest($2::text[])"
                " ON CONFLICT (session_id, enzyme_id) DO UPDATE SET injected_at = NOW()",
                session_id, enzyme_ids,
            )
            await conn.execute(
                "DELETE FROM prime_injections WHERE injected_at < NOW() - interval '1 day'"
            )

    async def record_prime_context(self, session_id: str, context: str,
                                   keep_seconds: float = 2 * 86400) -> None:
        """记下本轮 prime 的情境原文(已脱敏),顺手清掉过期行(默认保留两天)。"""
        if not session_id or not context:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO prime_contexts (session_id, context) VALUES ($1, $2)",
                session_id, context,
            )
            await conn.execute(
                "DELETE FROM prime_contexts WHERE created_at < NOW() - make_interval(secs => $1)",
                float(keep_seconds),
            )

    async def session_contexts(self, session_id: str, limit: int = 30) -> list[str]:
        """该会话最近的 prime 情境原文(新→旧,去重)。"""
        if not session_id:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT context FROM prime_contexts WHERE session_id=$1"
                " ORDER BY created_at DESC, id DESC LIMIT $2",
                session_id, int(limit),
            )
        return list(dict.fromkeys(r["context"] for r in rows))

    # ── Decision log(判定记录平台) ────────────────────────────

    async def insert_decision(self, rec: dict) -> None:
        """写一条判定记录(过期清理由归档循环负责:只删已归档的,见 app/distill/archive.py)。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO decision_log (id, session_id, project, domain, context_hash, policy, request,"
                " answers, candidates, injected) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)"
                " ON CONFLICT (id) DO NOTHING",
                rec["id"], rec.get("session_id", ""), rec.get("project", ""), rec.get("domain", ""),
                rec["context_hash"], json.dumps(rec["policy"], ensure_ascii=False),
                json.dumps(rec.get("request"), ensure_ascii=False),
                json.dumps(rec.get("answers"), ensure_ascii=False),
                json.dumps(rec["candidates"], ensure_ascii=False),
                json.dumps(rec["injected"], ensure_ascii=False),
            )

    async def insert_outcome(self, enzyme_id: str, outcome: str, source: str = "", decision_id: str = "",
                             session_id: str = "", context_hash: str = "") -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO decision_outcomes (decision_id, session_id, enzyme_id, outcome, source, context_hash)"
                " VALUES ($1,$2,$3,$4,$5,$6)",
                decision_id or "", session_id or "", enzyme_id, outcome, source or "", context_hash or "",
            )

    async def upsert_teacher_label(self, decision_id: str, teacher: str, answers: dict, usage: dict | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO teacher_labels (decision_id, teacher, answers, usage) VALUES ($1,$2,$3,$4)"
                " ON CONFLICT (decision_id, teacher) DO UPDATE SET answers = EXCLUDED.answers,"
                " usage = EXCLUDED.usage, created_at = NOW()",
                decision_id, teacher, json.dumps(answers, ensure_ascii=False), json.dumps(usage or {}),
            )

    async def fetch_decisions(self, since=None, limit: int = 1000, after_id: str = "",
                              unlabeled_by: str = "") -> list[dict]:
        """按时间顺序分页取判定记录;unlabeled_by 只取该老师还没标注过、且有判定请求的记录。"""
        conds, params = ["TRUE"], []
        if since is not None:
            params.append(since)
            conds.append(f"d.created_at >= ${len(params)}")
        if after_id:
            params.append(after_id)
            conds.append(f"(d.created_at, d.id) > (SELECT created_at, id FROM decision_log WHERE id = ${len(params)})")
        if unlabeled_by:
            params.append(unlabeled_by)
            conds.append(f"d.request IS NOT NULL AND d.request->'questions' <> '{{}}'::jsonb AND NOT EXISTS "
                         f"(SELECT 1 FROM teacher_labels t WHERE t.decision_id = d.id AND t.teacher = ${len(params)})")
        params.append(int(limit))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM decision_log d WHERE {' AND '.join(conds)}"
                f" ORDER BY d.created_at, d.id LIMIT ${len(params)}", *params)
        out = []
        for r in rows:
            d = dict(r)
            for k in ("policy", "request", "answers", "candidates", "injected"):
                if isinstance(d.get(k), str):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out

    async def fetch_outcomes(self, since=None) -> list[dict]:
        async with self._pool.acquire() as conn:
            if since is None:
                rows = await conn.fetch("SELECT * FROM decision_outcomes ORDER BY created_at")
            else:
                rows = await conn.fetch("SELECT * FROM decision_outcomes WHERE created_at >= $1"
                                        " ORDER BY created_at", since)
        return [dict(r) for r in rows]

    async def fetch_teacher_labels(self, decision_ids: list[str]) -> dict[str, dict[str, dict]]:
        if not decision_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT decision_id, teacher, answers FROM teacher_labels"
                                    " WHERE decision_id = ANY($1::text[])", decision_ids)
        out: dict[str, dict[str, dict]] = {}
        for r in rows:
            ans = r["answers"]
            out.setdefault(r["decision_id"], {})[r["teacher"]] = json.loads(ans) if isinstance(ans, str) else ans
        return out

    async def list_decisions(self, limit: int = 50, offset: int = 0, source: str = "",
                             judge: str = "") -> tuple[list[dict], int]:
        """后台判定记录列表(摘要,不含请求原文)。"""
        conds, params = ["TRUE"], []
        if source:
            params.append(source)
            conds.append(f"coalesce(policy->>'source', 'prime') = ${len(params)}")
        if judge:
            params.append(judge)
            conds.append(f"coalesce(policy->>'judge', '') = ${len(params)}")
        where = " AND ".join(conds)
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM decision_log WHERE {where}", *params)
            rows = await conn.fetch(
                f"""
                SELECT d.id, d.created_at, d.session_id, d.project, d.domain,
                       coalesce(d.policy->>'source', 'prime') AS source,
                       coalesce(d.policy->>'judge', '') AS judge,
                       coalesce(d.policy->>'model', '') AS model,
                       left(coalesce(d.request->>'state', ''), 240) AS state,
                       jsonb_array_length(d.candidates) AS candidates,
                       jsonb_array_length(d.injected) AS injected,
                       (SELECT count(*) FROM decision_outcomes o WHERE o.decision_id = d.id) AS outcomes,
                       (SELECT count(*) FROM teacher_labels t WHERE t.decision_id = d.id) AS labels
                FROM decision_log d WHERE {where}
                ORDER BY d.created_at DESC LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
                """, *params, limit, offset)
        return [dict(r) for r in rows], total

    async def get_decision(self, decision_id: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM decision_log WHERE id=$1", decision_id)
            if row is None:
                return None
            outcomes = await conn.fetch(
                "SELECT enzyme_id, outcome, source, created_at FROM decision_outcomes"
                " WHERE decision_id=$1 ORDER BY created_at", decision_id)
            labels = await conn.fetch(
                "SELECT teacher, answers, usage, created_at FROM teacher_labels WHERE decision_id=$1"
                " ORDER BY created_at", decision_id)
        rec = _json_row(row, ("policy", "request", "answers", "candidates", "injected"))
        rec["outcomes"] = [dict(o) for o in outcomes]
        rec["teacher_labels"] = [_json_row(r, ("answers", "usage")) for r in labels]
        return rec

    # ── Episodes(情景记忆) ────────────────────────────────────

    async def upsert_episode(self, episode: Episode) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO episodes (
                    id, session_id, occurred_at, domain, title, intent,
                    events, root_cause, outcome, lesson,
                    entities, error_signatures, cues, excerpt,
                    enzyme_ids, salience, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
                ON CONFLICT (id) DO UPDATE SET
                    title=$5, intent=$6, events=$7, root_cause=$8, outcome=$9,
                    lesson=$10, entities=$11, error_signatures=$12, cues=$13,
                    excerpt=$14, enzyme_ids=$15, salience=$16
                """,
                episode.id, episode.session_id, episode.occurred_at, episode.domain,
                episode.title, episode.intent,
                json.dumps(episode.events, ensure_ascii=False),
                episode.root_cause, episode.outcome, episode.lesson,
                episode.entities, episode.error_signatures, episode.cues,
                episode.excerpt, episode.enzyme_ids, episode.salience,
                episode.created_at,
            )

    async def get_episode(self, episode_id: str) -> Episode | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM episodes WHERE id = $1", episode_id)
        return _row_to_episode(row) if row else None

    async def recall_episodes(
        self,
        cues: list[str],
        domain: str | None = None,
        limit: int = 5,
    ) -> list[tuple[Episode, int]]:
        """模式补全:按线索重叠度召回历史情景,返回 (episode, overlap) 降序。

        回想不是单 query 最近邻,而是"残缺线索碎片 vs 完整情景"的交集评分——
        重叠越多越可能是同一幕;同分时新的、salience 高的优先。
        """
        cues = [c.strip().lower() for c in cues if c and c.strip()]
        if not cues:
            return []
        conditions = ["cues && $1::text[]"]
        params: list[Any] = [cues]
        if domain:
            params.append(domain)
            conditions.append(f"domain = ${len(params)}")
        params.append(limit)
        sql = f"""
            SELECT *,
                   (SELECT COUNT(*) FROM unnest(cues) c
                    WHERE c = ANY($1::text[])) AS overlap
            FROM episodes
            WHERE {' AND '.join(conditions)}
            ORDER BY overlap DESC, salience DESC, occurred_at DESC
            LIMIT ${len(params)}
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [(_row_to_episode(r), r["overlap"]) for r in rows]

    async def episodes_for_enzyme(self, enzyme_id: str, limit: int = 3) -> list[Episode]:
        """某条酶的出处情景(最近优先)——"这条直觉是从哪几幕里来的"。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM episodes WHERE $1 = ANY(enzyme_ids)"
                " ORDER BY occurred_at DESC LIMIT $2",
                enzyme_id, limit,
            )
        return [_row_to_episode(r) for r in rows]

    async def set_episode_ref(self, enzyme_id: str, ref: str) -> None:
        """把酶的出处指针写进 references(替换旧的 episode: 条目,不触发向量重嵌)。

        出处是展示信号不是语义变化,所以绕开 update_enzyme 的 version/updated_at/
        Qdrant 同步,只做一次轻量列更新——与 record_activations 同理。
        """
        async with self._pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT references_ FROM enzymes WHERE id = $1", enzyme_id)
            if raw is None:
                return
            refs = json.loads(raw) if isinstance(raw, str) else list(raw)
            refs = [r for r in refs if not str(r).startswith("episode:")]
            refs.append(ref)
            await conn.execute(
                "UPDATE enzymes SET references_ = $2 WHERE id = $1",
                enzyme_id, json.dumps(refs, ensure_ascii=False),
            )

    # ── App settings (KV) ───────────────────────────────────

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with self._pool.acquire() as conn:
            val = await conn.fetchval("SELECT value FROM app_settings WHERE key=$1", key)
            return val if val is not None else default

    async def get_settings(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM app_settings WHERE key = ANY($1::text[])",
                keys,
            )
            return {r["key"]: r["value"] for r in rows}

    # ── 语义去重判定缓存 ─────────────────────────────────────

    async def get_dedup_verdicts(self, pair_keys: list[str]) -> dict[str, tuple[str, float]]:
        if not pair_keys:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pair_key, input_hash, probability FROM dedup_verdicts WHERE pair_key = ANY($1::text[])",
                pair_keys,
            )
        return {r["pair_key"]: (r["input_hash"], float(r["probability"])) for r in rows}

    async def save_dedup_verdicts(self, rows: list[tuple[str, str, float]]) -> None:
        if not rows:
            return
        async with self._pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO dedup_verdicts (pair_key, input_hash, probability, judged_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (pair_key) DO UPDATE
                    SET input_hash = EXCLUDED.input_hash, probability = EXCLUDED.probability, judged_at = NOW()
                """,
                rows,
            )

    async def prune_dedup_verdicts(self, keep_keys: list[str]) -> int:
        """删掉已经不是候选对的旧判定(记忆删了 / 近邻变了),表的大小跟着候选对数量走。"""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM dedup_verdicts WHERE NOT (pair_key = ANY($1::text[]))", keep_keys)
        return int(result.split()[-1]) if result else 0

    # ── 领域表 ───────────────────────────────────────────────

    async def list_domains(self) -> list[dict]:
        """全部领域:名称、描述、来源、记忆条数。记忆里出现过、表里还没有的领域先补登记。"""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO domains (name, source) SELECT DISTINCT domain, 'existing' FROM enzymes "
                "WHERE domain <> '' ON CONFLICT (name) DO NOTHING")
            rows = await conn.fetch(
                "SELECT d.name, d.description, d.source, COUNT(e.id) AS size FROM domains d "
                "LEFT JOIN enzymes e ON e.domain = d.name "
                "GROUP BY d.name, d.description, d.source ORDER BY size DESC, d.name")
        return [{"name": r["name"], "description": r["description"], "source": r["source"],
                 "size": int(r["size"])} for r in rows]

    async def add_domain(self, name: str, description: str, source: str) -> bool:
        """新建领域;同名已存在时不改动,返回 False。"""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "INSERT INTO domains (name, description, source) VALUES ($1, $2, $3) ON CONFLICT (name) DO NOTHING",
                name, description, source)
        return result.endswith(" 1")

    async def describe_domain(self, name: str, description: str) -> None:
        """给还没有描述的领域补上描述(已有描述的不覆盖)。"""
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE domains SET description = $2 WHERE name = $1 AND description = ''",
                               name, description)

    async def domain_samples(self, name: str, limit: int = 5) -> list[str]:
        """领域里最有代表性的几条记忆(启用的、置信度与触发次数高的在前),给语言模型写领域描述用。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT then_action FROM enzymes WHERE domain = $1 "
                "ORDER BY (status = 'active') DESC, confidence DESC, trigger_count DESC LIMIT $2", name, limit)
        return [r["then_action"] for r in rows]

    async def routable_domains(self) -> list[dict]:
        """有启用记忆的领域(名称 + 描述),给召回时的领域分流当选项;只读,不补登记。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT d.name, d.description FROM domains d WHERE EXISTS "
                "(SELECT 1 FROM enzymes e WHERE e.domain = d.name AND e.status = 'active') ORDER BY d.name")
        return [{"name": r["name"], "description": r["description"]} for r in rows]

    async def delete_domain(self, name: str) -> bool:
        """删除领域——只在已经没有记忆属于它时才删。"""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM domains WHERE name = $1 AND NOT EXISTS (SELECT 1 FROM enzymes WHERE domain = $1)", name)
        return result.endswith(" 1")

    async def set_setting(self, key: str, value: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
                """,
                key, value,
            )

    async def get_settings_with_prefix(self, prefix: str) -> dict[str, tuple[str, datetime]]:
        """{键: (值, 修改时间)},只取给定前缀的行(运行参数 cfg:*)。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, updated_at FROM app_settings WHERE starts_with(key, $1)", prefix)
        return {r["key"]: (r["value"], r["updated_at"]) for r in rows}

    async def set_settings(self, values: dict[str, str], delete: list[str] | None = None) -> None:
        """一个事务里批量写入 / 删除,保证一次保存要么全生效要么全不生效。"""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for key, value in values.items():
                    await conn.execute(
                        "INSERT INTO app_settings (key, value, updated_at) VALUES ($1, $2, NOW())"
                        " ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()",
                        key, value,
                    )
                if delete:
                    await conn.execute("DELETE FROM app_settings WHERE key = ANY($1::text[])", delete)

    async def delete_settings(self, keys: list[str]) -> None:
        if keys:
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM app_settings WHERE key = ANY($1::text[])", keys)

    async def admin_fingerprint(self) -> tuple:
        """运行参数与模型注册表的整体指纹:多副本据此发现后台改动(见 app/runtime.watch)。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT (SELECT count(*) FROM app_settings WHERE starts_with(key, 'cfg:')) AS s_n,
                       (SELECT max(updated_at) FROM app_settings WHERE starts_with(key, 'cfg:')) AS s_ts,
                       (SELECT count(*) FROM model_providers) AS p_n,
                       (SELECT max(updated_at) FROM model_providers) AS p_ts,
                       (SELECT count(*) FROM model_entries) AS m_n,
                       (SELECT max(updated_at) FROM model_entries) AS m_ts
                """
            )
        return tuple(row.values())

    # ── Model registry(服务商 / 模型)────────────────────────

    async def list_providers(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM model_providers ORDER BY created_at, id")
        return [_json_row(r, ("headers",)) for r in rows]

    async def get_provider(self, provider_id: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM model_providers WHERE id=$1", provider_id)
        return _json_row(row, ("headers",)) if row else None

    async def create_provider(self, data: dict) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_providers (id, name, protocol, base_url, api_key, headers, enabled, note)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                RETURNING *
                """,
                data["id"], data.get("name", ""), data["protocol"], data.get("base_url", ""),
                data.get("api_key", ""), json.dumps(data.get("headers") or {}),
                bool(data.get("enabled", True)), data.get("note", ""),
            )
        return _json_row(row, ("headers",))

    async def update_provider(self, provider_id: str, fields: dict) -> dict | None:
        allowed = ("name", "protocol", "base_url", "api_key", "headers", "enabled", "note")
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return await self.get_provider(provider_id)
        cols = ", ".join(f"{k}=${i}" + ("::jsonb" if k == "headers" else "")
                         for i, k in enumerate(sets, start=2))
        vals = [json.dumps(v) if k == "headers" else v for k, v in sets.items()]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE model_providers SET {cols}, updated_at=NOW() WHERE id=$1 RETURNING *",
                provider_id, *vals)
        return _json_row(row, ("headers",)) if row else None

    async def delete_provider(self, provider_id: str, cascade: bool = False) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if cascade:
                    await conn.execute("DELETE FROM model_entries WHERE provider_id=$1", provider_id)
                result = await conn.execute("DELETE FROM model_providers WHERE id=$1", provider_id)
        return result == "DELETE 1"

    async def list_models(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM model_entries ORDER BY created_at, id")
        return [_json_row(r, ("params",)) for r in rows]

    async def get_model(self, model_id: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM model_entries WHERE id=$1", model_id)
        return _json_row(row, ("params",)) if row else None

    async def create_model(self, data: dict) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO model_entries (id, provider_id, kind, model, params, enabled, note)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                RETURNING *
                """,
                data["id"], data["provider_id"], data["kind"], data["model"],
                json.dumps(data.get("params") or {}), bool(data.get("enabled", True)), data.get("note", ""),
            )
        return _json_row(row, ("params",))

    async def update_model(self, model_id: str, fields: dict) -> dict | None:
        allowed = ("provider_id", "kind", "model", "params", "enabled", "note")
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return await self.get_model(model_id)
        cols = ", ".join(f"{k}=${i}" + ("::jsonb" if k == "params" else "")
                         for i, k in enumerate(sets, start=2))
        vals = [json.dumps(v) if k == "params" else v for k, v in sets.items()]
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE model_entries SET {cols}, updated_at=NOW() WHERE id=$1 RETURNING *",
                model_id, *vals)
        return _json_row(row, ("params",)) if row else None

    async def delete_model(self, model_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM model_entries WHERE id=$1", model_id)
        return result == "DELETE 1"

    # ── Audit log ─────────────────────────────────────────────

    async def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail) VALUES ($1, $2, $3, $4::jsonb)",
                actor or "", action, target or "", json.dumps(detail or {}, ensure_ascii=False, default=str),
            )

    async def list_audit(self, limit: int = 50, offset: int = 0, action: str = "", actor: str = "",
                         q: str = "") -> tuple[list[dict], int]:
        conds, params = ["TRUE"], []
        if action:
            params.append(action)
            conds.append(f"(action = ${len(params)} OR starts_with(action, ${len(params)} || '.'))")
        if actor:
            params.append(actor)
            conds.append(f"actor = ${len(params)}")
        if q:
            params.append(f"%{q}%")
            conds.append(f"(target ILIKE ${len(params)} OR detail::text ILIKE ${len(params)})")
        where = " AND ".join(conds)
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM audit_log WHERE {where}", *params)
            rows = await conn.fetch(
                f"SELECT * FROM audit_log WHERE {where} ORDER BY created_at DESC, id DESC"
                f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}", *params, limit, offset)
        return [_json_row(r, ("detail",)) for r in rows], total

    # ── Jobs / system overview ───────────────────────────────

    async def list_jobs(self, kind: str = "", status: str = "", limit: int = 50,
                        offset: int = 0) -> tuple[list[dict], int]:
        conds, params = ["TRUE"], []
        if kind:
            params.append(kind)
            conds.append(f"kind = ${len(params)}")
        if status:
            params.append(status)
            conds.append(f"status = ${len(params)}")
        where = " AND ".join(conds)
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM background_jobs WHERE {where}", *params)
            rows = await conn.fetch(
                f"SELECT id, kind, status, attempts, result, error, created_at, updated_at, finished_at,"
                f" payload->>'trigger' AS trigger, payload->>'session_id' AS session_id,"
                f" payload->>'project' AS project, length(payload->>'transcript') AS transcript_chars"
                f" FROM background_jobs WHERE {where} ORDER BY created_at DESC"
                f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}", *params, limit, offset)
        return [_json_row(r, ("result",)) for r in rows], total

    async def activity_series(self, days: int = 14) -> list[dict]:
        """近 N 天每天的活动量(总览曲线):prime 判定、事后结果(反馈)、capture 会话、进化事件。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH days AS (
                    SELECT generate_series(date_trunc('day', NOW()) - ($1::int - 1) * INTERVAL '1 day',
                                           date_trunc('day', NOW()), INTERVAL '1 day') AS day
                )
                SELECT d.day,
                       (SELECT count(*) FROM decision_log x
                        WHERE x.created_at >= d.day AND x.created_at < d.day + INTERVAL '1 day') AS decisions,
                       (SELECT count(*) FROM decision_outcomes o
                        WHERE o.created_at >= d.day AND o.created_at < d.day + INTERVAL '1 day') AS outcomes,
                       (SELECT count(*) FROM background_jobs j WHERE j.kind = 'capture'
                        AND j.created_at >= d.day AND j.created_at < d.day + INTERVAL '1 day') AS captures,
                       (SELECT count(*) FROM evolution_log e
                        WHERE e.created_at >= d.day AND e.created_at < d.day + INTERVAL '1 day') AS events
                FROM days d ORDER BY d.day
                """,
                days,
            )
        return [{"day": r["day"].date().isoformat(), "decisions": r["decisions"], "outcomes": r["outcomes"],
                 "captures": r["captures"], "events": r["events"]} for r in rows]

    async def home_summary(self, days: int = 7, series_days: int = 14, tz: str = "UTC") -> dict:
        """首页的数字:最近 N 天智能体提问时注入了多少条记忆、其中多少被证实有用 / 说反了、
        按天的序列,以及待审核数、近期失败的后台任务、最近一次提问与巩固的时间。

        注入只算真实提问的判定(来源 prime,MCP 的 prime 也记作 prime);回放、网关与评测的判定不算。
        反馈只算主反馈(report、report_batch 的首条、capture 的被动评估):批量反馈的回声是同一次
        反馈给连带记忆的折扣信用,网关结果与评测标准答案也不是智能体的真实结局,算进来会放大"有用"。

        按天的序列以看的人所在时区(tz,IANA 名称,调用方负责校验)切日:否则在 UTC-4 的晚上,
        图上最后一根柱子会标成明天。日界换回 timestamptz 再比较,created_at 上的索引照样能用。
        """
        injected = ("coalesce(sum(CASE WHEN jsonb_typeof(injected) = 'array'"
                    " THEN jsonb_array_length(injected) ELSE 0 END), 0)")
        is_prime = "coalesce(policy->>'source', 'prime') = 'prime'"
        async with self._pool.acquire() as conn:
            window = await conn.fetchrow(
                f"SELECT count(*) AS primes, {injected} AS injected FROM decision_log"
                f" WHERE created_at >= NOW() - $1::int * INTERVAL '1 day' AND {is_prime}",
                days,
            )
            feedback = await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE outcome = ANY($2::text[])) AS helped,"
                "       count(*) FILTER (WHERE outcome = ANY($3::text[])) AS harmed"
                " FROM decision_outcomes"
                " WHERE created_at >= NOW() - $1::int * INTERVAL '1 day' AND source = ANY($4::text[])",
                days, list(_HELPED_OUTCOMES), list(_HARMED_OUTCOMES), list(_FEEDBACK_SOURCES),
            )
            series = await conn.fetch(
                f"""
                WITH local_days AS (
                    SELECT generate_series(date_trunc('day', NOW() AT TIME ZONE $4) - ($1::int - 1) * INTERVAL '1 day',
                                           date_trunc('day', NOW() AT TIME ZONE $4), INTERVAL '1 day') AS day
                ), days AS (
                    SELECT day, day AT TIME ZONE $4 AS starts, (day + INTERVAL '1 day') AT TIME ZONE $4 AS ends
                    FROM local_days
                )
                SELECT d.day,
                       (SELECT {injected} FROM decision_log
                        WHERE created_at >= d.starts AND created_at < d.ends AND {is_prime}) AS injected,
                       (SELECT count(*) FROM decision_outcomes
                        WHERE created_at >= d.starts AND created_at < d.ends
                          AND outcome = ANY($2::text[]) AND source = ANY($3::text[])) AS helped
                FROM days d ORDER BY d.day
                """,
                series_days, list(_HELPED_OUTCOMES), list(_FEEDBACK_SOURCES), tz,
            )
            # 判定记录可能被关掉或已过保留期:退回到记忆上的"最近一次被注入"
            last_prime = await conn.fetchval(
                f"SELECT greatest((SELECT max(created_at) FROM decision_log WHERE {is_prime}),"
                f"                (SELECT max(last_activated) FROM enzymes))"
            )
            last_consolidation = await conn.fetchval(
                "SELECT max(finished_at) FROM background_jobs WHERE kind = 'consolidate' AND status = 'succeeded'"
            )
            pending_review = await conn.fetchval("SELECT count(*) FROM enzymes WHERE status = 'quarantine'")
            failed_jobs = await conn.fetchval(
                "SELECT count(*) FROM background_jobs WHERE status = 'failed'"
                " AND coalesce(finished_at, updated_at) >= NOW() - $1::int * INTERVAL '1 day'",
                days,
            )
        return {
            "impact": {"days": days, "primes": window["primes"], "injected": window["injected"],
                       "helped": feedback["helped"], "harmed": feedback["harmed"]},
            "series": [{"day": r["day"].date().isoformat(), "injected": r["injected"], "helped": r["helped"]}
                       for r in series],
            "tz": tz,
            "last_prime_at": last_prime.isoformat() if last_prime else None,
            "last_consolidation_at": last_consolidation.isoformat() if last_consolidation else None,
            "pending_review": pending_review or 0,
            "failed_jobs": failed_jobs or 0,
        }

    async def job_counts(self) -> dict[str, dict[str, int]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT kind, status, count(*) AS n FROM background_jobs GROUP BY 1, 2")
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        return out

    async def retry_job(self, job_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE background_jobs SET status='pending', error='', updated_at=NOW(), finished_at=NULL"
                " WHERE id=$1 AND status='failed' AND (kind <> 'capture' OR payload <> '{}'::jsonb)", job_id)
        return result == "UPDATE 1"

    async def table_sizes(self) -> dict[str, dict]:
        """各表行数与占用空间(系统页用):大表用 pg_class 估计值,小表(或从未 ANALYZE 的表)精确计数。"""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.relname AS name, c.reltuples::bigint AS rows, pg_total_relation_size(c.oid) AS bytes
                FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r' AND n.nspname = current_schema()
                ORDER BY pg_total_relation_size(c.oid) DESC
                """
            )
            out = {}
            for r in rows:
                n = int(r["rows"])
                if n < 100_000:
                    n = await conn.fetchval(f'SELECT count(*) FROM "{r["name"]}"')
                out[r["name"]] = {"rows": n, "bytes": int(r["bytes"])}
        return out

    async def pg_version(self) -> str:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SHOW server_version")

    # ── User CRUD ───────────────────────────────────────────

    async def count_users(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users")

    async def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        display_name: str = "",
        role: str = "user",
    ) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (id, username, password_hash, display_name, role)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                user_id, username, password_hash, display_name or username, role,
            )
            return dict(row)

    async def get_user_by_username(self, username: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username)
            return dict(row) if row else None

    async def get_user_by_id(self, user_id: str) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM users WHERE id=$1", user_id)
            return dict(row) if row else None

    async def update_user(self, user_id: str, **fields) -> dict | None:
        """Patch a user. Allowed: display_name, avatar_url, password_hash, role, is_active."""
        allowed = {"display_name", "avatar_url", "password_hash", "role", "is_active"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return await self.get_user_by_id(user_id)
        cols = ", ".join(f"{k}=${i}" for i, k in enumerate(sets, start=2))
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE users SET {cols}, updated_at=NOW() WHERE id=$1 RETURNING *",
                user_id, *sets.values(),
            )
            return dict(row) if row else None

    async def list_users(self) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users ORDER BY created_at")
            return [dict(r) for r in rows]

    async def delete_user(self, user_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id=$1", user_id)
        return result == "DELETE 1"

    async def count_active_admins(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT count(*) FROM users WHERE role='admin' AND is_active")

    # ── Enzyme CRUD ─────────────────────────────────────────

    async def upsert_enzyme(self, enzyme: Enzyme, expected_version: int | None = None) -> int:
        """写入记忆并入队向量同步,返回本次写入的 outbox generation。

        ``expected_version`` 非空时启用乐观锁:仅当库里版本号等于读取时的版本才更新,
        否则抛 :class:`ConcurrencyConflict`(调用方重读重试)。用于 hebbian 反馈这类
        读-改-写并发热点,防止两个并发更新互相覆盖计数/置信度。
        """
        sql = """
                    INSERT INTO enzymes (
                        id, type, version, patterns, conditions, negations,
                        then_action, because, severity, references_,
                        confidence, precision_, status, quality_score,
                        trigger_count, success_count,
                        false_positive_count, last_triggered,
                        origin, parent_ids, domain, tags, created_at, updated_at,
                        activation_count, last_activated, exemplars, anti_exemplars
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        type=$2, version=$3, patterns=$4, conditions=$5, negations=$6,
                        then_action=$7, because=$8, severity=$9, references_=$10,
                        confidence=$11, precision_=$12, status=$13, quality_score=$14,
                        trigger_count=$15, success_count=$16,
                        false_positive_count=$17, last_triggered=$18,
                        origin=$19, parent_ids=$20, domain=$21, tags=$22, updated_at=$24,
                        activation_count=$25, last_activated=$26, exemplars=$27, anti_exemplars=$28
                    """
        params: list = [
            enzyme.id,
            enzyme.type.value,
            enzyme.version,
            json.dumps([p.model_dump() for p in enzyme.patterns]),
            json.dumps(enzyme.conditions),
            json.dumps(enzyme.negations),
            enzyme.then,
            enzyme.because,
            enzyme.severity.value,
            json.dumps(enzyme.references),
            enzyme.confidence,
            enzyme.precision,
            enzyme.status,
            enzyme.quality_score,
            enzyme.trigger_count,
            enzyme.success_count,
            enzyme.false_positive_count,
            enzyme.last_triggered,
            enzyme.origin.value,
            enzyme.parent_ids,
            enzyme.domain,
            enzyme.tags,
            enzyme.created_at,
            enzyme.updated_at,
            enzyme.activation_count,
            enzyme.last_activated,
            json.dumps(enzyme.exemplars, ensure_ascii=False),
            json.dumps(enzyme.anti_exemplars, ensure_ascii=False),
        ]
        if expected_version is not None:
            sql += " WHERE enzymes.version = $29"
            params.append(expected_version)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(sql, *params)
                if expected_version is not None and result == "INSERT 0 0":
                    # 乐观锁冲突:不写入队行,交调用方重读重试
                    raise ConcurrencyConflict(enzyme.id)
                generation = await conn.fetchval(
                    """
                    INSERT INTO vector_sync_queue(enzyme_id, operation, attempts, last_error, updated_at)
                    VALUES($1, 'upsert', 0, '', NOW())
                    ON CONFLICT (enzyme_id) DO UPDATE SET
                        operation='upsert',
                        generation=vector_sync_queue.generation+1,
                        attempts=0, last_error='', updated_at=NOW()
                    RETURNING generation
                    """,
                    enzyme.id,
                )
        return int(generation)

    async def record_activations(self, enzyme_ids: list[str]) -> None:
        """Record that these enzymes were matched & injected by prime().

        Lightweight batched UPDATE — deliberately does NOT touch version,
        updated_at, or the Qdrant vector (activation is a usage signal, not a
        semantic change). This is the closed-loop counterpart to report():
        prime() supplies the dense "was used" signal, report() the sparse
        "did it help" signal.
        """
        if not enzyme_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE enzymes
                SET activation_count = activation_count + 1,
                    last_activated = NOW()
                WHERE id = ANY($1::text[])
                """,
                enzyme_ids,
            )

    async def get_enzyme(self, enzyme_id: str) -> Enzyme | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM enzymes WHERE id = $1", enzyme_id)
        return _row_to_enzyme(row) if row else None

    async def get_enzymes(self, enzyme_ids: list[str]) -> list[Enzyme]:
        """按 id 集合一次取回,返回顺序与传入 enzyme_ids 一致(缺失的跳过)。

        召回顺序在下游是承重的:无 rerank 回退路径下 refined 保持召回序,
        最终稳定排序在 (tier,-score) 并列时靠它决定名次——所以不能依赖
        ANY() 的返回序,必须按入参重排。
        """
        if not enzyme_ids:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM enzymes WHERE id = ANY($1::text[])", enzyme_ids
            )
        by_id = {r["id"]: _row_to_enzyme(r) for r in rows}
        return [by_id[eid] for eid in enzyme_ids if eid in by_id]

    async def list_enzymes(
        self,
        domain: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> list[Enzyme]:
        clauses = ["confidence >= $1"]
        params: list[Any] = [min_confidence]
        idx = 2

        if domain:
            clauses.append(f"domain = ${idx}")
            params.append(domain)
            idx += 1

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM enzymes WHERE {where} ORDER BY confidence DESC LIMIT ${idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_enzyme(r) for r in rows]

    _ENZYME_SORTS = {
        "confidence": "confidence", "precision": "precision_", "triggers": "trigger_count",
        "activations": "activation_count", "created_at": "created_at", "updated_at": "updated_at",
        "quality": "quality_score", "last_activated": "last_activated",
    }

    async def query_enzymes(
        self,
        q: str = "",
        domain: str = "",
        status: str = "",
        origin: str = "",
        severity: str = "",
        min_confidence: float = 0.0,
        sort: str = "confidence",
        desc: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Enzyme], int]:
        """后台记忆表:文本搜索 + 多条件过滤 + 排序 + 分页,返回(本页, 总数)。"""
        conds, params = ["confidence >= $1"], [min_confidence]
        for col, val in (("domain", domain), ("status", status), ("origin", origin), ("severity", severity)):
            if val:
                params.append(val)
                conds.append(f"{col} = ${len(params)}")
        if q:
            params.append(f"%{q}%")
            n = len(params)
            conds.append(f"(id ILIKE ${n} OR then_action ILIKE ${n} OR because ILIKE ${n}"
                         f" OR patterns::text ILIKE ${n} OR domain ILIKE ${n} OR array_to_string(tags, ' ') ILIKE ${n})")
        where = " AND ".join(conds)
        col = self._ENZYME_SORTS.get(sort, "confidence")
        order = f"{col} {'DESC' if desc else 'ASC'} NULLS LAST, id"
        async with self._pool.acquire() as conn:
            total = await conn.fetchval(f"SELECT count(*) FROM enzymes WHERE {where}", *params)
            rows = await conn.fetch(
                f"SELECT * FROM enzymes WHERE {where} ORDER BY {order}"
                f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}", *params, limit, offset)
        return [_row_to_enzyme(r) for r in rows], total

    async def enzyme_facets(self) -> dict[str, dict[str, int]]:
        async with self._pool.acquire() as conn:
            out = {}
            for col in ("domain", "status", "origin", "severity"):
                rows = await conn.fetch(
                    f"SELECT coalesce({col}, '') AS k, count(*) AS n FROM enzymes GROUP BY 1 ORDER BY 2 DESC")
                out[col] = {r["k"]: r["n"] for r in rows}
        return out

    async def list_all_enzymes(self) -> list[Enzyme]:
        """Return the complete authoritative set for index repair."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM enzymes ORDER BY id")
        return [_row_to_enzyme(row) for row in rows]

    async def top_mature(
        self,
        min_confidence: float = 0.75,
        min_precision: float = 0.7,
        limit: int = 10,
    ) -> list[Enzyme]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM enzymes
                WHERE confidence >= $1
                  AND precision_ >= $2
                  AND trigger_count > 5
                ORDER BY confidence * precision_ DESC
                LIMIT $3
                """,
                min_confidence,
                min_precision,
                limit,
            )
        return [_row_to_enzyme(r) for r in rows]

    async def delete_enzyme(self, enzyme_id: str) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute("DELETE FROM enzymes WHERE id = $1", enzyme_id)
                if result == "DELETE 1":
                    await conn.execute(
                        """
                        INSERT INTO vector_sync_queue(enzyme_id, operation, attempts, last_error, updated_at)
                        VALUES($1, 'delete', 0, '', NOW())
                        ON CONFLICT (enzyme_id) DO UPDATE SET
                            operation='delete',
                            generation=vector_sync_queue.generation+1,
                            attempts=0, last_error='', updated_at=NOW()
                        """,
                        enzyme_id,
                    )
        return result == "DELETE 1"

    async def count_enzymes(self) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM enzymes")

    async def stats(self) -> dict:
        async with self._pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM enzymes")
            by_origin = await conn.fetch(
                "SELECT origin, COUNT(*) as cnt FROM enzymes GROUP BY origin"
            )
            by_domain = await conn.fetch(
                "SELECT domain, COUNT(*) as cnt FROM enzymes GROUP BY domain ORDER BY cnt DESC LIMIT 10"
            )
            avg_conf = await conn.fetchval("SELECT AVG(confidence) FROM enzymes")
            total_triggers = await conn.fetchval("SELECT SUM(trigger_count) FROM enzymes")
            total_activations = await conn.fetchval("SELECT SUM(activation_count) FROM enzymes")
        return {
            "total_enzymes": total,
            "by_origin": {r["origin"]: r["cnt"] for r in by_origin},
            "top_domains": {r["domain"]: r["cnt"] for r in by_domain},
            "avg_confidence": round(float(avg_conf or 0), 3),
            "total_triggers": total_triggers or 0,
            "total_activations": total_activations or 0,
        }

    async def metrics(self) -> dict:
        """Evaluation metrics — make the learning loop falsifiable.

        Surfaces whether the system is actually learning, instead of just how
        many enzymes exist. Key signals:
        - feedback_coverage: of enzymes prime() has used, how many ever got a
          report() back. Low → the closed loop is starving (the #1 design risk).
        - activation_weighted_precision: precision weighted by how often each
          enzyme is actually used — what the agent really experiences.
        - false_positive_rate: cry-wolf rate across all reported triggers.
        - corrective vs reinforcing: recent Hebbian events by delta sign.
        """
        async with self._pool.acquire() as conn:
            activated = await conn.fetchval(
                "SELECT COUNT(*) FROM enzymes WHERE activation_count > 0"
            ) or 0
            activated_and_reported = await conn.fetchval(
                "SELECT COUNT(*) FROM enzymes WHERE activation_count > 0 AND trigger_count > 0"
            ) or 0
            total_triggers = await conn.fetchval("SELECT SUM(trigger_count) FROM enzymes") or 0
            total_fp = await conn.fetchval("SELECT SUM(false_positive_count) FROM enzymes") or 0
            # Activation-weighted precision over enzymes that have been reported
            wp = await conn.fetchrow(
                """
                SELECT SUM(precision_ * activation_count) AS num,
                       SUM(activation_count) AS den
                FROM enzymes WHERE trigger_count > 0
                """
            )
            by_status = await conn.fetch(
                "SELECT status, COUNT(*) AS cnt FROM enzymes GROUP BY status"
            )
            # Recent Hebbian learning direction (last 30 days)
            hebbian = await conn.fetch(
                """
                SELECT
                    SUM(CASE WHEN (detail->>'delta')::float > 0 THEN 1 ELSE 0 END) AS reinforcing,
                    SUM(CASE WHEN (detail->>'delta')::float < 0 THEN 1 ELSE 0 END) AS corrective,
                    SUM(CASE WHEN detail->>'learned_negation' IS NOT NULL
                             AND detail->>'learned_negation' <> 'null' THEN 1 ELSE 0 END) AS negations_learned
                FROM evolution_log
                WHERE event_type = 'hebbian'
                  AND created_at > NOW() - INTERVAL '30 days'
                """
            )
            speculative = await conn.fetchval(
                """
                SELECT COUNT(*) FROM enzymes
                WHERE status = 'active' AND trigger_count = 0
                  AND origin IN ('mutated', 'evolved') AND confidence < 0.35
                """
            ) or 0
            replay = await conn.fetchrow(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN verdict='helped' THEN 1 ELSE 0 END) AS helped,
                       SUM(CASE WHEN verdict='contradicted' THEN 1 ELSE 0 END) AS contradicted
                FROM replay_evals
                WHERE created_at > NOW() - INTERVAL '30 days'
                """
            )

        num = float(wp["num"]) if wp and wp["num"] is not None else 0.0
        den = float(wp["den"]) if wp and wp["den"] is not None else 0.0
        h = hebbian[0] if hebbian else {}
        return {
            "feedback_coverage": round(activated_and_reported / activated, 3) if activated else 0.0,
            "activated_enzymes": activated,
            "activated_and_reported": activated_and_reported,
            "activation_weighted_precision": round(num / den, 3) if den else None,
            "false_positive_rate": round(total_fp / total_triggers, 3) if total_triggers else None,
            "by_status": {r["status"]: r["cnt"] for r in by_status},
            "hebbian_30d": {
                "reinforcing": (h["reinforcing"] or 0) if h else 0,
                "corrective": (h["corrective"] or 0) if h else 0,
                "negations_learned": (h["negations_learned"] or 0) if h else 0,
            },
            "speculative_active": speculative,
            "replay_30d": {
                "total": (replay["total"] or 0) if replay else 0,
                "helped": (replay["helped"] or 0) if replay else 0,
                "contradicted": (replay["contradicted"] or 0) if replay else 0,
            },
        }

    # ── Relations ───────────────────────────────────────────

    async def upsert_relation(self, rel: EnzymeRelation) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO enzyme_relations (source_id, target_id, relation, weight)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (source_id, target_id, relation) DO UPDATE SET weight = $4
                """,
                rel.source_id,
                rel.target_id,
                rel.relation.value,
                rel.weight,
            )

    async def upsert_co_trigger_pairs(self, pairs: list[tuple[str, str]]) -> None:
        """批量记录共触发(调用方给出双向对)。weight 是共现计数,每次 +1
        (crossover 的 min_weight>=2 依赖这个语义);单事务 + executemany,
        避免 prime 热路径上 45 对 ×2 方向的逐条往返打满连接池。"""
        if not pairs:
            return
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO enzyme_relations (source_id, target_id, relation, weight)
                    VALUES ($1, $2, 'co_triggered', 1.0)
                    ON CONFLICT (source_id, target_id, relation) DO UPDATE SET
                        weight = enzyme_relations.weight + 1.0
                    """,
                    pairs,
                )

    async def get_related_batch(
        self, enzyme_ids: list[str], limit: int = 5
    ) -> dict[str, list[dict]]:
        """get_related(hops=1) 的批量版:每个 source 各取 top-limit
        (按 e.confidence*r.weight 降序,与单条版同语义;并列截断同样未指定
        顺序,与单条版的 ORDER BY...LIMIT 行为一致)。"""
        if not enzyme_ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM (
                    SELECT e.*, r.relation, r.weight AS rel_weight, r.source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY r.source_id
                               ORDER BY e.confidence * r.weight DESC
                           ) AS rn
                    FROM enzyme_relations r
                    JOIN enzymes e ON e.id = r.target_id
                    WHERE r.source_id = ANY($1::text[])
                ) t WHERE rn <= $2
                ORDER BY source_id, rn
                """,
                enzyme_ids,
                limit,
            )
        out: dict[str, list[dict]] = {eid: [] for eid in enzyme_ids}
        for r in rows:
            d = dict(r)
            d.pop("rn", None)
            out[d.pop("source_id")].append(d)
        return out

    async def get_related(self, enzyme_id: str, hops: int = 2, limit: int = 10) -> list[dict]:
        """1-2 跳关联查询。"""
        async with self._pool.acquire() as conn:
            if hops == 1:
                rows = await conn.fetch(
                    """
                    SELECT e.*, r.relation, r.weight AS rel_weight
                    FROM enzyme_relations r
                    JOIN enzymes e ON e.id = r.target_id
                    WHERE r.source_id = $1
                    ORDER BY e.confidence * r.weight DESC
                    LIMIT $2
                    """,
                    enzyme_id,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (e.id) e.*, 'direct' AS hop, r.weight AS rel_weight
                    FROM enzyme_relations r
                    JOIN enzymes e ON e.id = r.target_id
                    WHERE r.source_id = $1
                    UNION ALL
                    SELECT DISTINCT ON (e2.id) e2.*, '2hop' AS hop, r2.weight * 0.5 AS rel_weight
                    FROM enzyme_relations r1
                    JOIN enzyme_relations r2 ON r1.target_id = r2.source_id
                    JOIN enzymes e2 ON r2.target_id = e2.id
                    WHERE r1.source_id = $1
                      AND e2.id != $1
                    ORDER BY rel_weight DESC
                    LIMIT $2
                    """,
                    enzyme_id,
                    limit,
                )
        return [dict(r) for r in rows]

    # ── Evolution Log ───────────────────────────────────────

    async def log_event(self, event: EvolutionEvent) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO evolution_log (enzyme_id, event_type, detail, created_at)
                VALUES ($1, $2, $3, $4)
                """,
                event.enzyme_id,
                event.event_type.value,
                json.dumps(event.detail),
                event.created_at,
            )

    # ── Sessions ────────────────────────────────────────────

    async def create_session(
        self, session_id: str, workspace: str, features: dict, injected: list[str],
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (id, workspace, features, enzymes_injected)
                VALUES ($1, $2, $3, $4)
                """,
                session_id,
                workspace,
                json.dumps(features),
                injected,
            )

    # ── Knowledge Extraction ─────────────────────────────────

    async def keyword_search(
        self,
        keywords: list[str],
        domain: str | None = None,
        limit: int = 50,
    ) -> list[Enzyme]:
        """Fallback keyword search directly against PG patterns JSONB.

        Used when Meilisearch is unavailable or returns no results.
        Matches enzymes whose patterns, then_action, or because contain any keyword.
        """
        if not keywords:
            return []

        # Build OR conditions for each keyword across pattern values and text fields
        conditions = []
        params: list[Any] = []
        idx = 1
        for kw in keywords[:10]:  # Limit to 10 keywords to avoid huge queries
            conditions.append(
                f"(patterns::text ILIKE ${idx} OR then_action ILIKE ${idx} OR because ILIKE ${idx})"
            )
            params.append(f"%{kw}%")
            idx += 1

        where = " OR ".join(conditions)

        if domain:
            where = f"({where}) AND domain = ${idx}"
            params.append(domain)
            idx += 1

        sql = f"SELECT * FROM enzymes WHERE {where} ORDER BY confidence DESC LIMIT ${idx}"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_enzyme(r) for r in rows]

    async def get_known_terms(self, min_confidence: float = 0.5, limit: int = 200) -> list[str]:
        """Extract unique pattern keywords from mature enzymes.

        Used by genesis to replace hardcoded term lists — the enzyme database
        itself becomes the source of domain knowledge (self-bootstrapping).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT p->>'value' AS term
                FROM enzymes, jsonb_array_elements(patterns) AS p
                WHERE confidence >= $1
                  AND p->>'type' = 'keyword'
                  AND length(p->>'value') > 1
                ORDER BY term
                LIMIT $2
                """,
                min_confidence,
                limit,
            )
        return [r["term"] for r in rows if r["term"]]

    # ── Bulk ────────────────────────────────────────────────

    async def bulk_upsert(self, enzymes: list[Enzyme]) -> int:
        for e in enzymes:
            await self.upsert_enzyme(e)
        return len(enzymes)

    async def all_enzyme_ids(self) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT id FROM enzymes")
        return [r["id"] for r in rows]


# ── Helpers ────────────────────────────────────────────────────

def _json_row(row: asyncpg.Record, json_cols: tuple[str, ...]) -> dict:
    data = dict(row)
    for key in json_cols:
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return data


def _job_row_to_dict(row: asyncpg.Record) -> dict:
    data = dict(row)
    for key in ("payload", "result"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = {}
    return data


def _row_to_episode(row: asyncpg.Record) -> Episode:
    data = dict(row)
    events = data.get("events")
    if isinstance(events, str):
        events = json.loads(events)
    return Episode(
        id=data["id"],
        session_id=data.get("session_id") or "",
        occurred_at=data["occurred_at"],
        domain=data.get("domain") or "",
        title=data["title"],
        intent=data.get("intent") or "",
        events=events or [],
        root_cause=data.get("root_cause") or "",
        outcome=data.get("outcome") or "",
        lesson=data.get("lesson") or "",
        entities=list(data.get("entities") or []),
        error_signatures=list(data.get("error_signatures") or []),
        cues=list(data.get("cues") or []),
        excerpt=data.get("excerpt") or "",
        enzyme_ids=list(data.get("enzyme_ids") or []),
        salience=data.get("salience", 0.5) or 0.5,
        created_at=data["created_at"],
    )


def _row_to_enzyme(row: asyncpg.Record) -> Enzyme:
    data = dict(row)
    # JSONB → Python
    patterns_raw = data.pop("patterns")
    if isinstance(patterns_raw, str):
        patterns_raw = json.loads(patterns_raw)
    patterns = [Pattern(**p) for p in patterns_raw]

    conditions = data.pop("conditions")
    if isinstance(conditions, str):
        conditions = json.loads(conditions)

    negations = data.pop("negations")
    if isinstance(negations, str):
        negations = json.loads(negations)

    references = data.pop("references_")
    if isinstance(references, str):
        references = json.loads(references)

    precision = data.pop("precision_")

    exemplars = data.pop("exemplars", None) or []
    if isinstance(exemplars, str):
        exemplars = json.loads(exemplars)
    anti_exemplars = data.pop("anti_exemplars", None) or []
    if isinstance(anti_exemplars, str):
        anti_exemplars = json.loads(anti_exemplars)

    return Enzyme(
        id=data["id"],
        type=data["type"],
        version=data["version"],
        patterns=patterns,
        conditions=conditions,
        negations=negations,
        then=data["then_action"],
        because=data["because"] or "",
        severity=data["severity"],
        references=references,
        confidence=data["confidence"],
        precision=precision,
        status=data.get("status", "active") or "active",
        quality_score=data.get("quality_score", 0.0) or 0.0,
        trigger_count=data["trigger_count"],
        success_count=data["success_count"],
        false_positive_count=data["false_positive_count"],
        last_triggered=data["last_triggered"],
        activation_count=data.get("activation_count", 0) or 0,
        last_activated=data.get("last_activated"),
        origin=data["origin"],
        parent_ids=list(data["parent_ids"]) if data["parent_ids"] else [],
        domain=data["domain"] or "",
        tags=list(data["tags"]) if data["tags"] else [],
        exemplars=list(exemplars),
        anti_exemplars=list(anti_exemplars),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
