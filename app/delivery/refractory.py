"""Refractory — 会话内注入不应期(神经元同款机制),PG 共享态。

同一条酶在同一会话里刚被注入过,窗口期内不再重复注入:
- 治 token 噪音(同样的建议每轮刷屏);
- 治激活统计自相关(被抑制的注入不计 activation)。

例外:critical 严重度永远放行,真警报没有不应期。
状态存 PG(prime_injections 表),多进程/重启共享;全程 best-effort——
PG 抖动时宁可重复注入,也绝不让不应期阻断 prime 热路径。

客户端能读到上下文时(Claude Code hook 读 transcript),还会带上 seen_ids:
当前上下文里仍可见的注入。这些不看时间窗、不依赖 session_id,一律不再注入;
上下文被压缩后客户端不再上报,酶才可能重新注入。
"""

from __future__ import annotations

import logging

from app import settings
from app.core import Severity

logger = logging.getLogger(__name__)


async def filter_refractory(
    pg, session_id: str, results: list, window_seconds: float | None = None,
    seen_ids: list[str] | None = None,
) -> list:
    """滤掉上下文里已可见(seen_ids)或窗口期内已注入过的非 critical 结果。"""
    if seen_ids:
        seen = set(seen_ids)
        results = [r for r in results
                   if r.enzyme.id not in seen or r.enzyme.severity == Severity.CRITICAL]
    if not session_id or not results:
        return results
    window = window_seconds if window_seconds is not None else settings.get("ranking.refractory_seconds")
    try:
        recent = await pg.recent_injections(
            session_id, [r.enzyme.id for r in results], window)
    except Exception:
        logger.debug("refractory query failed (non-fatal)", exc_info=True)
        return results
    return [
        r for r in results
        if r.enzyme.id not in recent or r.enzyme.severity == Severity.CRITICAL
    ]


async def mark_injections(pg, session_id: str, enzyme_ids: list[str]) -> None:
    """记录本轮实际注入了谁(best-effort)。"""
    if not session_id or not enzyme_ids:
        return
    try:
        await pg.record_injections(session_id, enzyme_ids)
    except Exception:
        logger.debug("refractory mark failed (non-fatal)", exc_info=True)
