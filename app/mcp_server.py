"""MCP Server — FastMCP Streamable HTTP, standalone.

只暴露 5 个核心 Tool：prime / search / recall / report / harvest
管理类操作（list, get, stats, consolidate, import）走 REST API

定位：hook 是"推"（自动反射），MCP 是"拉"（agent 自主的查/忆/报/存）。
report 是学习环 outcome 信号的唯一主动入口，也是 MCP 存在的核心理由。
recall 是情景层(episodic memory)的主动回想入口:酶给"该做什么",情景给
"当时发生了什么"。
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastmcp import FastMCP, Context
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from app.config import Config
from app.core.engine import Engine
from app.delivery.formatter import format_match_results_slim
from app.storage.qdrant_store import QdrantStore
from app.storage.pg_store import PGStore

load_dotenv()  # standalone 启动时也能读到 .env

logger = logging.getLogger(__name__)

# When MCP is hosted by app.main it shares the REST Engine and therefore the
# same pools, clients and live configuration.  The standalone path below is
# retained for embedding the MCP app on its own.
_shared_engine: Engine | None = None


def set_shared_engine(engine: Engine | None) -> None:
    global _shared_engine
    _shared_engine = engine


# ── Auth ──────────────────────────────────────────────────────

def build_mcp_verifier(token: str) -> StaticTokenVerifier | None:
    """根据 token 构造静态 Bearer 校验器；token 为空则返回 None（不启用）。"""
    token = (token or "").strip()
    if not token:
        return None
    return StaticTokenVerifier(
        tokens={token: {"client_id": "cognitive-enzyme", "scopes": []}},
    )


def apply_mcp_auth(token: str) -> None:
    """在启动时（DB 加载后）设置 MCP 认证。运行中换令牌见 update_mcp_token(启用 / 关闭认证本身需重启)。

    注意：FastMCP 在缺少 Authorization 头时直接 401，无法用"放行型"校验器
    表达"关闭"，因此开关只能在启动时通过挂/不挂校验器决定。
    """
    verifier = build_mcp_verifier(token)
    mcp.auth = verifier
    if verifier:
        logger.info("MCP Server 已启用 Bearer Token 认证。")
    else:
        logger.warning(
            "MCP 未设置令牌 —— MCP Server 以【无认证】模式运行，"
            "任何能访问该端口的人都可调用。可在设置页或 CE_MCP_AUTH_TOKEN 配置。"
        )


def mcp_auth_active() -> bool:
    """MCP 当前是否挂了 Bearer 校验器(启动时决定)。"""
    return mcp.auth is not None


def update_mcp_token(token: str) -> bool:
    """运行中更换 MCP 令牌。返回 True = 已即时生效;False = 需要重启服务。

    已启用认证时换令牌即时生效(校验器每次请求都读令牌表);清空令牌时旧令牌立即作废
    (令牌表清空,重启后才切回无认证);从无认证到启用认证需要重启(校验器只能在启动时挂)。"""
    token = (token or "").strip()
    verifier = mcp.auth
    if isinstance(verifier, StaticTokenVerifier):
        verifier.tokens = {token: {"client_id": "cognitive-enzyme", "scopes": []}} if token else {}
        return bool(token)
    return not token


# ── Lifespan ──────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage DB connections lifecycle."""
    if _shared_engine is not None:
        yield {
            "engine": _shared_engine,
            "pg": _shared_engine.pg,
            "qdrant": _shared_engine.qdrant,
            "config": _shared_engine.config,
        }
        return

    base_config = Config.from_env()

    pg = PGStore(base_config.pg)
    await pg.connect()
    await pg.init_schema()

    from app import runtime
    config = await runtime.boot(pg)
    from app.llm_client import set_default_configs
    set_default_configs(config.llm, config.embedding)

    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    collection_created = await qdrant.ensure_collection()

    engine = Engine(config, pg, qdrant)
    if collection_created:
        await pg.enqueue_all_vector_sync()

    env_mcp_token = os.getenv("CE_MCP_AUTH_TOKEN", "").strip()
    mcp_token = await pg.get_setting("mcp_auth_token", "") or env_mcp_token
    apply_mcp_auth(mcp_token)

    from app.background import vector_sync_worker
    sync_task = asyncio.create_task(vector_sync_worker(engine))

    try:
        yield {"engine": engine, "pg": pg, "qdrant": qdrant, "config": config}
    finally:
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
        await qdrant.close()
        await pg.close()


def _get_engine(ctx: Context) -> Engine:
    return ctx.lifespan_context["engine"]


# ── Server ────────────────────────────────────────────────────

mcp = FastMCP(
    "cognitive-enzyme",
    instructions="认知酶：AI 的 System 1 直觉层。使用 prime() 获取安全直觉，search() 自主检索记忆，recall() 回想历史情景，report() 反馈结果，harvest() 沉淀经验。",
    lifespan=lifespan,
)
# 认证在 REST 启动流程里通过 apply_mcp_auth(token) 设置（token 来自 DB / env）。


# ── Core Tools ────────────────────────────────────────────────

@mcp.tool(annotations={
    "title": "Prime — 上下文直觉匹配",
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
async def prime(
    context: str,
    ctx: Context,
    domain: str = "",
    max_results: int = 15,
) -> dict:
    """分析当前上下文，召回相关的安全直觉与建议（System 1 快思考）。

    动手前调用：把你正面对的任务 / 代码 / 情境描述放进 context，拿回一组按相关度
    排序的直觉。用完后请调用 report() 反馈是否有用，以驱动这些直觉持续进化。

    参数：
      context:     当前上下文描述，越具体召回越准
      domain:      可选。限定领域（如 'python'、'security'）以缩小召回范围
      max_results: 可选。返回条数上限，默认 15

    每条 match 含：id（report() 时回传）、suggestion（建议动作）、reason（依据）、
    severity（严重度）、confidence（置信度 0–1）、scope（general 方法论提示 / specific
    具体经验）、references（参考，可选）。
    """
    engine = _get_engine(ctx)
    results = await engine.match(context, domain=domain or None, max_results=max_results)

    if results:
        await engine.record_activations([r.enzyme.id for r in results])

    if len(results) > 1:
        ids = [r.enzyme.id for r in results[:10]]
        await engine.record_co_triggers(ids)

    formatted = format_match_results_slim(results)
    return {"matches": formatted, "total": len(formatted)}


@mcp.tool(annotations={
    "title": "Search — 自主检索记忆库",
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
async def search(
    query: str,
    ctx: Context,
    domain: str = "",
    limit: int = 10,
) -> dict:
    """按你自拟的查询词检索酶库（语义向量优先，未配 embedding 时退化为关键词匹配）。

    与 prime() 的区别：prime 是把当前情境整体交给系统做直觉匹配，会记录激活信号、
    参与学习统计；search 是任务中途你**自主构造查询**的纯检索，不影响学习信号。
    适合：调试时查某个具体主题的既有经验、动手前查项目约定/踩坑记录。
    若采纳了某条结果的建议，请随后调用 report([该条 id], success) 反馈效果。

    参数：
      query:  查询词或一句话描述，越具体越准
      domain: 可选。限定领域（如 'python'、'security'）
      limit:  可选。返回条数上限，默认 10
    """
    engine = _get_engine(ctx)
    from app.llm_client import embed
    vector = await embed(query, config=engine.config.embedding)
    if vector:
        hits = await engine.qdrant.search(vector, domain=domain or None, limit=limit)
    else:
        enzymes = await engine.pg.keyword_search([query], domain=domain or None, limit=limit)
        hits = [
            {"id": e.id, "then": e.then, "because": e.because,
             "patterns_text": e.patterns_readable}
            for e in enzymes
        ]
    return {"hits": hits, "total": len(hits)}


@mcp.tool(annotations={
    "title": "Recall — 回想历史情景",
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
})
async def recall(
    ctx: Context,
    cues: list[str] | None = None,
    query: str = "",
    domain: str = "",
    limit: int = 5,
    episode_id: str = "",
) -> dict:
    """回想"当时发生了什么"——按线索碎片把历史上最像的几幕情景调回来(模式补全)。

    与 prime()/search() 的区别:那两个返回提炼后的规则("该做什么"),recall 返回
    带出处的完整一幕(当时的意图、事件链、根因、结局、原文摘录)。适合:似曾相识但
    想不起细节时("我们上次是怎么解决的?")、prime 结果里带 provenance 想看整幕时、
    需要和上次做类比("这次哪里不一样")时。

    参数:
      cues:       线索碎片列表(报错关键词/文件名/组件名),手头有什么给什么
      query:      或者给一句自由描述,自动抽线索
      domain:     可选。限定领域
      limit:      可选。返回幕数上限,默认 5
      episode_id: 钻取模式——给某一幕的 id,返回整幕细节(事件链/报错原文/摘录)

    返回骨架列表(title/intent/outcome/root_cause/lesson);想看细节用 episode_id 钻取。
    """
    engine = _get_engine(ctx)
    from app.delivery.formatter import format_episode_detail, format_episode_skeleton
    from app.learning.episodes import cues_from_text

    if episode_id:
        episode = await engine.pg.get_episode(episode_id)
        if not episode:
            return {"error": "episode not found"}
        return format_episode_detail(episode)

    merged = list(cues or [])
    if query:
        merged += cues_from_text(query)
    if not merged:
        return {"error": "需要 cues 或 query 至少一项"}
    hits = await engine.pg.recall_episodes(
        merged, domain=domain or None, limit=max(1, min(limit, 20)),
    )
    return {
        "episodes": [format_episode_skeleton(ep, overlap) for ep, overlap in hits],
        "total": len(hits),
        "tip": "想看某幕细节,用 recall(episode_id=...) 钻取。",
    }


@mcp.tool(annotations={
    "title": "Report — 反馈 prime 直觉的效果",
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
})
async def report(
    enzyme_ids: list[str],
    success: bool,
    ctx: Context,
    detail: str = "",
    unexpected: bool = False,
) -> dict:
    """反馈上一次 prime() 的直觉是否帮到了你 —— 驱动学习进化的关键一步。

    把 prime() 返回的 id 按原排名顺序放进 enzyme_ids：排第一的拿满信用并更新统计，
    其余按排名递减获得置信度回声，无需逐条判断到底哪条起了作用。只反馈一条时
    传单元素列表即可。

    参数：
      enzyme_ids: prime() 返回的酶 id 列表（保持原排名顺序）
      success:    这批直觉整体上是否有用 / 判断是否正确
      detail:     可选。失败时尽量写明哪里不对，系统据此学习抑制条件
      unexpected: 可选。若 success 且结果出乎意料（本不该命中却命中了），置 True
                  触发探索性学习，可能催生更聚焦的新酶
    """
    if not enzyme_ids:
        return {"error": "enzyme_ids 不能为空"}
    engine = _get_engine(ctx)
    expected = False if unexpected else None
    result = await engine.report_batch(
        enzyme_ids=enzyme_ids, success=success, detail=detail, expected=expected,
    )
    if success and unexpected:
        result["tip"] = "意外成功！考虑调用 harvest() 把这个发现沉淀成新酶。"
    return result


@mcp.tool(annotations={
    "title": "Harvest — 沉淀经验",
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
})
async def harvest(
    insight: str,
    context: str,
    ctx: Context,
    domain: str = "",
    tags: list[str] | None = None,
) -> dict:
    """把刚学到的有价值经验主动沉淀为一条新酶，供未来 prime() 召回。

    当你解决了一个问题、踩了一个坑、或发现一条值得复用的规律时调用。会先过质量
    门槛：通过则存入，可改进则自动润色后存入，存疑则隔离待审，不达标则拒收并给改进建议。

    参数：
      insight: 要沉淀的经验本身（“下次遇到 X 应该 Y”这类可复用的结论）
      context: 这条经验是在什么情境下得出的，帮助未来精准召回
      domain:  可选。所属领域（如 'python'、'security'）
      tags:    可选。标签列表，便于检索归类
    """
    engine = _get_engine(ctx)
    return await engine.harvest(insight=insight, context=context, domain=domain, tags=tags or [])
