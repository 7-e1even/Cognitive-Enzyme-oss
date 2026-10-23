"""领域自动归类——新记忆入库时归入已有领域,都不合适时由语言模型新建。

原先的领域靠提炼时语言模型随手填(常常为空,同一类事每次叫法不同),既当不了统计口径,
也当不了合并、召回的范围。现在入库(harvest:REST、MCP、capture 的 ADD 都经过)时:

1. 判官(Jev)一道单选题:这条记忆最适合收进哪个已有领域(选项 = 领域名 + 一句话描述),
   都不合适选 none。最高项 ≥ domain.assign_q 且高于 none 就归进去。
2. 都不合适,由语言模型看着已有领域表新建一个大类(名称 + 描述);它认定其实属于已有领域的,
   就用那个。判官不可用时直接走这一步,由语言模型同时做"挑已有 / 新建"。
3. 领域数到上限(domain.max_count)后不再新建,归入判官认为最接近的领域。
4. 巩固时收拾零散的小领域(merge_small_domains):成员很少的领域逐条重新归类,归得进成规模的领域就并过去。

已有领域没有描述的(导入数据、旧记忆里的领域),第一次用到时由语言模型读几条样例补一句描述。
任何一步失败都退回调用方给的领域(原行为),入库不因归类失败受影响。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from app import settings
from app.config import TypeSafeConfig
from app.core import Enzyme, EventType, EvolutionEvent
from app.llm_client import chat_json
from app.storage.pg_store import PGStore
from app.typesafe_client import _OPTION_DESC_MAX, _RULE_THEN_MAX, fit_options, system_one

logger = logging.getLogger(__name__)

_ASSIGN_QUESTION = "state 是一条记忆。以下哪个领域最适合收录它?如果都不合适,选 none。"
_NONE_OPTION = "都不合适,需要新建一个领域"
_SYSTEM = "你负责维护一个经验记忆库的领域分类。只返回 JSON。"


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _option(domain: dict, desc_max: int = 0) -> str:
    desc = domain.get("description") or ""
    if desc_max:
        desc = desc[:desc_max]
    return f"{domain['name']}:{desc}" if desc else domain["name"]


async def describe(name: str, samples: list[str]) -> str:
    """读领域里的几条记忆,写一句收录范围。"""
    if not samples:
        return ""
    listing = "\n".join(f"- {s[:300]}" for s in samples)
    result = await chat_json(system=_SYSTEM, user=f"""领域「{name}」里有这些记忆:
{listing}

用一句话(20~40 字)说明这个领域收录什么内容。写收录范围,不要复述某一条记忆。
返回 {{"description": "..."}}""")
    if not isinstance(result, dict):
        return ""
    return str(result.get("description") or "").strip()[:200]


async def taxonomy(pg: PGStore) -> list[dict]:
    """当前领域表;还没有描述的领域并发补上描述(每个领域只需一次)。"""
    domains = await pg.list_domains()
    missing = [d for d in domains if not d["description"]]

    async def fill(domain: dict) -> None:
        text = await describe(domain["name"], await pg.domain_samples(domain["name"]))
        if text:
            domain["description"] = text
            await pg.describe_domain(domain["name"], text)

    if missing:
        await asyncio.gather(*(fill(d) for d in missing), return_exceptions=True)
    return domains


async def judge_domain(rule: dict, domains: list[dict], config: TypeSafeConfig) -> dict | None:
    """判官单选:{closest, q, none, pick}。pick = 达到归入门槛的领域,否则 None;判官失败返回 None。"""
    if not domains:
        return None
    # 判官单次请求有 token 硬上限:规则字段按 _rule 同款上限截断,选项太多时从尾部丢。
    state = {"memory": {k: (v[:_RULE_THEN_MAX] if isinstance(v, str) else v) for k, v in rule.items()}}
    options = {f"d{i}": _option(d, _OPTION_DESC_MAX) for i, d in enumerate(domains)}
    options["none"] = _NONE_OPTION
    dropped = fit_options(state, options, "d")
    if dropped:
        logger.debug("Domain judge 超 token 预算:领域选项 %d → %d 个", len(domains), len(domains) - dropped)
        domains = domains[:len(domains) - dropped]
    data = await system_one(state,
                            {"domain": {"type": "choice", "instructions": _ASSIGN_QUESTION, "criteria": options}},
                            config, timeout=15.0)
    try:
        probs = {k: float(v) for k, v in data["answers"]["domain"]["probabilities"].items()} if data else None
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Domain judge response malformed: %s", e)
        return None
    if not probs:
        return None
    best = max((k for k in options if k != "none"), key=lambda k: probs.get(k, 0.0))
    q, none = probs.get(best, 0.0), probs.get("none", 0.0)
    closest = domains[int(best[1:])]["name"]
    pick = closest if q >= settings.get("domain.assign_q") and q > none else None
    return {"closest": closest, "q": round(q, 3), "none": round(none, 3), "pick": pick}


async def propose_domain(rule: dict, domains: list[dict], hint: str = "") -> dict | None:
    """语言模型看着已有领域表:属于已有的返回原名,都不属于就新建一个大类。返回 {name, description}。"""
    listing = "\n".join(f"- {_option(d)}" for d in domains) or "(还没有任何领域)"
    hint_line = f"\n提炼时给的领域名(仅供参考):{hint}\n" if hint else ""
    result = await chat_json(system=_SYSTEM, user=f"""已有领域:
{listing}

新记忆:
{json.dumps(rule, ensure_ascii=False)}
{hint_line}
判断这条记忆属于哪个领域:
- 属于某个已有领域 → 返回那个领域的原名;
- 都不属于 → 新建一个领域。领域是大类(例如「前端工程」「法律」「生物医学」),不要细到具体任务、
  项目或工具版本;名称 2~8 个字;描述用一句话说明收录范围。

返回 {{"name": "领域名", "description": "一句话描述"}}""")
    if not isinstance(result, dict):
        return None
    name = re.sub(r"\s+", " ", str(result.get("name") or "")).strip()
    if not name or len(name) > 40:
        return None
    return {"name": name, "description": str(result.get("description") or "").strip()[:200]}


async def resolve_domain(pg: PGStore, insight: str, context: str, hint: str,
                         typesafe_config: TypeSafeConfig | None) -> tuple[str, dict]:
    """新记忆该放进哪个领域 → (领域名, 归类经过)。关闭或失败时返回调用方给的 hint(原行为)。"""
    hint = (hint or "").strip()
    if not settings.get("domain.auto"):
        return hint, {"via": "off"}
    domains = await taxonomy(pg)
    by_norm = {_norm(d["name"]): d["name"] for d in domains}
    if hint and _norm(hint) in by_norm:
        return by_norm[_norm(hint)], {"via": "given"}

    rule = {"情境": context[:300], "内容": insight[:600]}
    judged = await judge_domain(rule, domains, typesafe_config) if typesafe_config is not None else None
    scores = {k: judged[k] for k in ("closest", "q", "none")} if judged else {}
    if judged and judged["pick"]:
        return judged["pick"], {"via": "judge", **scores}
    at_cap = len(domains) >= settings.get("domain.max_count")
    if at_cap and judged:
        return judged["closest"], {"via": "closest", **scores}

    proposal = await propose_domain(rule, domains, hint)
    if not proposal:
        return hint, {"via": "failed", **scores}
    existing = by_norm.get(_norm(proposal["name"]))
    if existing:
        return existing, {"via": "llm", **scores}
    if at_cap:   # 判官不可用又到了上限:不新建
        return hint, {"via": "cap", **scores}
    await pg.add_domain(proposal["name"], proposal["description"], "created")
    logger.info("Created domain %r: %s", proposal["name"], proposal["description"])
    return proposal["name"], {"via": "created", "description": proposal["description"], **scores}


def _rule_of(enzyme: Enzyme) -> dict:
    return {"情境": enzyme.patterns_readable[:300], "内容": enzyme.then[:600]}


async def merge_small_domains(pg: PGStore, sync, config: TypeSafeConfig) -> int:
    """巩固时收拾零散的小领域:成员不超过 domain.merge_below 条的领域,逐条请判官在成规模的领域里重新归类,
    达到归入门槛就搬过去;搬空的领域删掉。只信判官,判官不可用时不动。返回搬动的记忆条数。"""
    limit = settings.get("domain.merge_below")
    if not settings.get("domain.auto") or limit <= 0:
        return 0
    domains = await taxonomy(pg)
    targets = [d for d in domains if d["size"] > limit]   # 只往成规模的领域并,不在小领域之间来回搬
    moved = 0
    for small in [d for d in domains if 0 < d["size"] <= limit]:
        if not targets:
            break
        for enzyme in await pg.list_enzymes(domain=small["name"], limit=limit):
            judged = await judge_domain(_rule_of(enzyme), targets, config)
            if not judged or not judged["pick"]:
                continue
            enzyme.domain = judged["pick"]
            enzyme.updated_at = datetime.now(timezone.utc)
            await sync.sync_enzyme(enzyme, await pg.upsert_enzyme(enzyme))
            await pg.log_event(EvolutionEvent(
                enzyme_id=enzyme.id, event_type=EventType.CONSOLIDATION,
                detail={"action": "move_domain", "from": small["name"], "to": judged["pick"], "q": judged["q"]}))
            moved += 1
        if await pg.delete_domain(small["name"]):
            logger.info("Merged small domain %r into larger ones", small["name"])
    return moved
