"""召回时的领域分流(类似 MoE 的门控):判官判断这次提问要用到哪些领域,召回再到这些领域里找。

- 判官(Jev)一道单选题:state 是当前提问,选项是各领域(名称 + 一句话描述)与 none(都不需要 / 通用问题)。
  单选概率 ≥ recall.domain_route_q 且高于 none 的领域按概率从高到低选中,最多 recall.domain_route_k 个。
- 与向量检索并行发出;检索完成后最多再等 recall.domain_route_timeout 秒,超时、判官不可用、
  判为通用问题都照常全库召回——分流只会多找,不会让召回因为判官出问题而变差(严格模式除外)。
- 两种用法(recall.domain_route):expert = 全库召回照常,再到选中的领域里各补一轮检索(领域专家);
  strict = 只留选中领域里的候选。
- 领域表来自领域自动归类(app/learning/domains.py),只取有启用记忆的领域,进程内缓存 60 秒。
"""

from __future__ import annotations

import asyncio
import logging
import time

from app import settings
from app.config import TypeSafeConfig
from app.typesafe_client import _OPTION_DESC_MAX, _STATE_MAX, fit_options, system_one

logger = logging.getLogger(__name__)

_QUESTION = "state 是智能体当前的提问。处理它需要用到以下哪个领域的知识?如果都不需要,或只是通用问题,选 none。"
_NONE = "都不需要,或只是通用问题"
_CACHE_TTL = 60.0
_cache: tuple[float, list[dict]] | None = None


def route_mode() -> str:
    return settings.get("recall.domain_route")


async def _options(pg) -> list[dict]:
    global _cache
    now = time.monotonic()
    if _cache is None or _cache[0] < now:
        _cache = (now + _CACHE_TTL, await pg.routable_domains())
    return _cache[1]


async def pick_domains(context: str, pg, config: TypeSafeConfig | None) -> dict | None:
    """→ {"domains": [(名称, 概率), ...], "none": 概率};判官不可用 / 出错 / 没有领域时返回 None。"""
    if config is None or not config.available:
        return None
    domains = await _options(pg)
    if len(domains) < 2:        # 只有一个领域,分不分流都一样
        return None
    state = context[:_STATE_MAX]
    options = {f"d{i}": (f"{d['name']}:{d['description'][:_OPTION_DESC_MAX]}" if d["description"] else d["name"])
               for i, d in enumerate(domains)}
    options["none"] = _NONE
    dropped = fit_options(state, options, "d")   # 领域太多/描述太胖时从尾部丢选项,别整次 422
    if dropped:
        logger.debug("Domain route 超 token 预算:领域选项 %d → %d 个", len(domains), len(domains) - dropped)
        domains = domains[:len(domains) - dropped]
    data = await system_one(state,
                            {"route": {"type": "choice", "instructions": _QUESTION, "criteria": options}},
                            config, timeout=10.0)
    try:
        probs = {k: float(v) for k, v in data["answers"]["route"]["probabilities"].items()} if data else None
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Domain route answers malformed: %s", e)
        return None
    if not probs:
        return None
    none = probs.get("none", 0.0)
    threshold = settings.get("recall.domain_route_q")
    # 判官可能返回幻觉/过期选项键(如 d17 但只有 5 个领域):跳过,别整轮路由崩掉
    def _domain_of(key: str) -> dict | None:
        if not key.startswith("d") or not key[1:].isdigit():
            return None
        idx = int(key[1:])
        return domains[idx] if 0 <= idx < len(domains) else None

    ranked = sorted(
        (item for item in
         ((_domain_of(k), p) for k, p in probs.items() if k != "none")
         if item[0] is not None),
        key=lambda x: -x[1],
    )
    chosen = [(d["name"], round(p, 3)) for d, p in ranked if p >= threshold and p > none]
    return {"domains": chosen[:settings.get("recall.domain_route_k")], "none": round(none, 3)}


async def await_route(task: asyncio.Task | None) -> dict | None:
    """检索完成后等分流结果,最多等 recall.domain_route_timeout 秒;超时或出错按没有分流处理。"""
    if task is None:
        return None
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=settings.get("recall.domain_route_timeout"))
    except asyncio.TimeoutError:
        task.cancel()
        logger.info("Domain route timed out; recalling from all domains")
        return None
    except Exception as e:
        logger.warning("Domain route failed: %s", e)
        return None
