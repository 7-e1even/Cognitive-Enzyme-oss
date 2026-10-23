"""Capture — 从 agent 会话 transcript 中提取记忆并与已有酶对账(B 路线)。

读路径(prime)是 System 1 同步注入;本模块是 System 2 异步写入:
会话结束 / 压缩前由 hook 触发,把整段对话提炼成候选记忆,再逐条与库里
已有的酶对账,路由到 ADD / UPDATE / REINFORCE / INVALIDATE / SKIP。

设计要点:
- **提取 ≠ 对账**:先抽候选,再撞库,再让 LLM 只对"撞上的"做归并裁决。
- **确定性预过滤**:没撞到任何相似酶的候选直接走 harvest(ADD),不浪费一次 LLM。
- **复用既有机制**:ADD→engine.harvest(已会过滤+质量门+dedup),REINFORCE/INVALIDATE→
  engine.hebbian_update(维持/纠错+负学习),UPDATE→直接增补 then/because。
- 全程 best-effort:任何一条候选失败都不影响其余;整体绝不抛给调用方。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app import settings
from app.core import EvolutionEvent, EventType
from app.core.engine import Engine
from app.learning.privacy import scrub_secrets
from app.learning.transcript import clip_transcript
from app.llm_client import chat_json, embed

logger = logging.getLogger(__name__)

# 撞库召回参数
_SIM_LIMIT = 5            # 每条候选最多召回几条相似酶
_SIM_MIN_SCORE = 0.55     # 低于此余弦分视为"没撞上" → 直接 ADD
_MAX_CANDIDATES = 8       # 单次会话最多处理的候选数(控成本)
_MAX_EVAL = 12            # 单次会话被动评估的注入酶上限


# ── 注入痕迹解析(被动闭环前置)──────────────────────────────

# prime hook 注入块的三种形态:哨兵块(新)、SEEN 压缩行(capture hook 端压缩)、
# 无哨兵的旧格式块(按首行标题 + 尾行提示语定界)。
_INJECT_BLOCK_RE = re.compile(r"⟦CE:BEGIN⟧.*?⟦CE:END⟧", re.DOTALL)
_SEEN_LINE_RE = re.compile(r"⟦CE:SEEN ([^⟧]*)⟧")
_LEGACY_BLOCK_RE = re.compile(
    r"\[认知酶 · System 1 直觉\].*?(?:主动检索。|以强化/纠正它。)", re.DOTALL
)
_ID_RE = re.compile(r"‹enzyme_id=([^›\s]+)›")


def split_injections(transcript: str) -> tuple[str, list[str]]:
    """剥离 prime 注入痕迹,返回 (干净文本, 注入过的酶 id 列表)。

    防回声:注入的酶文本若留在 transcript 里进 extract,会被重新提炼成候选、
    撞库命中自身、被 reconcile 判 REINFORCE —— 酶靠自己的注入无证据地自我强化。
    因此必须在提取前整块剥离;收集到的 id 交给 evaluate_injections 做效果评估。
    """
    if not transcript:
        return "", []
    # id 只从注入块/SEEN 行里收集:正文(用户贴的 debug 输出、agent 讨论某条酶)
    # 出现的 ‹enzyme_id=…› 不是注入,收进来会产生虚假的被动反馈
    ids: list[str] = []
    for block_re in (_INJECT_BLOCK_RE, _LEGACY_BLOCK_RE):
        for m in block_re.finditer(transcript):
            ids += _ID_RE.findall(m.group(0))
    for m in _SEEN_LINE_RE.finditer(transcript):
        ids += [i.strip() for i in m.group(1).split(",") if i.strip()]
    clean = _INJECT_BLOCK_RE.sub("", transcript)
    clean = _LEGACY_BLOCK_RE.sub("", clean)
    clean = _SEEN_LINE_RE.sub("", clean)
    return clean, list(dict.fromkeys(ids))


# ── 阶段 1:EXTRACT ────────────────────────────────────────────

async def extract_candidates(transcript: str, domain: str = "") -> list[dict]:
    """从 transcript 提炼出候选记忆(还没入库)。"""
    transcript = (transcript or "").strip()
    if not transcript:
        return []

    prompt = f"""下面是一次 AI 智能体会话的对话记录(编程、解题、分析等都有可能)。请提取其中**跨会话仍然有用**的经验,
做成可复用的「直觉规则」候选。每条规则描述:在什么情况下(trigger)、应该做什么(then)、为什么(because)。

只提取下列类型,且必须是"下次遇到类似情况能直接复用"的:
- 架构/技术选型决策(及其理由)
- 踩过的坑 / bug 根因 / 修复套路
- 项目约定、规范、工作流偏好
- 用户明确表达的偏好或要求
- 领域方法:某类问题真正起作用的方法、关键想法、有用的已知结论(写清适用条件)
- 通用方法论:遇到问题时怎么拆解、怎么排查、怎么验证——不依赖具体项目或题目,换一个领域也用得上

记录里的 [tool_error] 行是工具真实执行失败的报错原文,[tool_ok] 行是之后成功执行的命令。对"报错 → 后来修好"的片段(同类命令先 [tool_error]、后 [tool_ok],或助手明确说明已解决):
- 提炼成"遇到这种报错时怎么修":trigger 必须原样包含报错里稳定的关键文本(错误类型、错误信息主干;去掉路径、行号、时间戳、随机 id),then 写真正起作用的修法,because 写根因;
- 只提炼后来确实修好了的;报错之后没有修好、或只是绕过去的,不要提炼;
- 这类经验在 tags 里加 "kind:fix"。

**排除**:一次性的细节、与具体某行代码或某个具体数值绑定的临时信息、闲聊、空泛的套话(如"仔细审题""认真检查")。
方法和方法论都要写清在什么情况下、具体怎么做。

对话记录:
\"\"\"
{clip_transcript(transcript)}
\"\"\"

返回 JSON 数组,每个元素:
{{"trigger": "触发情境的一句话描述", "then": "该做什么(可复用的行动/建议)", "because": "原因", "domain": "{domain}", "tags": ["可选标签"], "salience": 0.0到1.0的重要度, "scope": "general(通用方法论,跨领域可用)或 specific"}}

只返回最有价值的 1-{_MAX_CANDIDATES} 条,宁缺毋滥。没有值得记的就返回 []。"""

    result = await chat_json(
        system="你是资深工程师,负责从对话中沉淀可复用的工程直觉。只返回 JSON 数组。",
        user=prompt,
    )
    if not isinstance(result, list):
        return []

    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        then = (item.get("then") or "").strip()
        if not then:
            continue
        tags = [t for t in (item.get("tags") or []) if isinstance(t, str)]
        if str(item.get("scope") or "").strip().lower() == "general" and "scope:general" not in tags:
            tags.append("scope:general")      # 通用方法论:跨领域可用,与具体领域的经验区分开
        out.append({
            "trigger": (item.get("trigger") or "").strip(),
            "then": then,
            "because": (item.get("because") or "").strip(),
            "domain": (item.get("domain") or domain or "").strip(),
            "tags": tags,
            "salience": float(item.get("salience", 0.5) or 0.0),
        })
    return out


# ── 撞库:找相似的已有酶 ───────────────────────────────────────

async def _find_similar(engine: Engine, cand: dict) -> list[dict]:
    """对候选做向量检索,返回相似的已有酶(含 id / then / because)。"""
    text = f"{cand['then']} {cand.get('because', '')} {cand.get('trigger', '')}"
    try:
        vector = await embed(text, config=engine.config.embedding)
    except Exception:
        vector = None
    if not vector:
        return []
    try:
        hits = await engine.qdrant.search(vector, limit=_SIM_LIMIT, include_all=True)
    except Exception:
        logger.debug("capture similar-search failed (non-fatal)", exc_info=True)
        return []
    return [h for h in hits if isinstance(h, dict) and h.get("_score", 0) >= _SIM_MIN_SCORE]


# ── 阶段 2:RECONCILE ──────────────────────────────────────────

async def reconcile(cand: dict, hits: list[dict]) -> dict | None:
    """让 LLM 在「候选 vs 命中的旧酶」之间做归并裁决,返回一条操作。"""
    existing_block = "\n".join(
        f"- id={h.get('id')} | then: {h.get('then', '')} | because: {h.get('because', '')}"
        for h in hits
    )
    prompt = f"""有一条新提取的「直觉规则」候选,以及库里若干条相似的已有规则。请判断该如何处置候选。

候选:
- then: {cand['then']}
- because: {cand.get('because', '')}
- trigger: {cand.get('trigger', '')}

库里相似的已有规则:
{existing_block}

判定规则(择一):
- "ADD"        候选是全新认知,和上面任何一条都不是同一件事 → 新增
- "UPDATE"     候选是某条旧规则的细化/补充,同一主题 → 增补到那条(给 target_id 和更完整的 then/because;then 会直接替换旧规则的正文,必须写出合并后的完整正文,不要写『保留原文』『增补如下』之类的说明)
- "REINFORCE"  候选只是再次印证了某条旧规则,无新信息 → 强化那条(给 target_id)
- "INVALIDATE" 候选**推翻/矛盾**于某条旧规则 → 作废旧的并新增(给 target_id;新内容放 then/because)
- "SKIP"       候选无价值或纯重复,且不值得强化 → 丢弃

只返回 JSON 对象:
{{"action": "ADD|UPDATE|REINFORCE|INVALIDATE|SKIP", "target_id": "命中的id或null", "then": "最终采用的then", "because": "最终采用的because", "reason": "一句话理由"}}"""

    result = await chat_json(
        system="你负责维护一个记忆库的一致性,谨慎裁决新旧记忆的关系。只返回 JSON 对象。",
        user=prompt,
    )
    return result if isinstance(result, dict) else None


# ── APPLY:把裁决落到引擎 ──────────────────────────────────────

# LLM 对 UPDATE 有时只回"补丁说明 + 补充段"而不是合并后的完整正文(如
# 「(原then全部保留不变:……)新增条款……」)——照存会把旧正文丢光、且无从恢复
# (实测某库 59/524 条中招)。检出这种开头括注就把补充段追加到旧正文后面。
_PATCH_NOTE = re.compile(r"^(?:（(.{0,300}?)）|\((.{0,300}?)\))")   # 收尾括号与开头同款:全角括注里常夹半角 (i)
_PATCH_WORDS = ("保留", "不变", "增补", "追加", "补充")


def _merge_then(old: str, new: str) -> tuple[str, str]:
    """UPDATE 的 then 归并:返回 (最终正文, mode)。

    补丁式回复(开头括注含「保留/不变/增补/追加/补充」)→ 补充段追加到旧正文后面,
    mode="append";只有说明没有内容 → 不动,mode="noop";正常返回完整正文照旧替换,
    mode="replace"。"""
    m = _PATCH_NOTE.match(new.strip())
    if m and any(w in (m.group(1) or m.group(2)) for w in _PATCH_WORDS):
        addendum = new.strip()[m.end():].strip()
        if addendum:
            return old.rstrip() + "\n\n" + addendum, "append"
        return old, "noop"
    return new or old, "replace"


def _count_new(summary: dict, res, key: str = "added") -> None:
    """新增类 harvest 的计数:返回 status=="known" 记 known,否则记 key。

    用 .get 而不是直接索引 —— 外部调用方(测试/评测脚本)传进来的 summary 可能没有 "known" 键。
    """
    if isinstance(res, dict) and res.get("status") == "known":
        summary["known"] = summary.get("known", 0) + 1
    else:
        summary[key] += 1


async def _learn_from_session(engine: Engine, enzyme, trigger: str, session_id: str, source: str,
                              reveal: bool = True) -> None:
    """会话里本该想起这条记忆 → 记一条经验样本。优先用会话里它真正适用的那一轮 prime 情境原文
    (与之后 prime 的查询同分布,真实经验流上"事后得知"学的就是它);挑不出来(没有会话情境 /
    没配 Jev / Jev 判都不适用)再退回 LLM 概括的触发描述。"""
    try:
        text = None
        ts = engine.config.typesafe
        if session_id and ts.available:
            from app.typesafe_client import pick_context
            contexts = await engine.pg.session_contexts(session_id)
            text = await pick_context(enzyme, contexts, ts) if contexts else None
        if text:
            await engine.learn_exemplar(enzyme.id, text, source=f"{source}_context")
        elif trigger:
            await engine.learn_exemplar(enzyme.id, trigger, source=source)
        if reveal:  # 判定记录:这次会话本该想起它(挑出的情境原文据哈希找回当时那次判定)
            await engine.record_outcome(enzyme.id, "revealed", source, session_id=session_id, context=text or "")
    except Exception:  # 经验样本是附带学习,任何失败都不影响归并/评估主流程
        logger.debug("exemplar learning from session failed (non-fatal)", exc_info=True)


async def _apply(
    engine: Engine, cand: dict, decision: dict, summary: dict,
    touched: list[str] | None = None, session_id: str = "",
) -> None:
    action = (decision.get("action") or "SKIP").upper()
    target_id = decision.get("target_id")
    # 统一入口的密钥擦除:capture 的转录虽已脱敏,但裁决文本来自 LLM、
    # ingest 的文档分块根本没脱敏——写库前在这里兜底(harvest 另有自己一道)
    then = scrub_secrets((decision.get("then") or cand["then"]).strip())
    because = scrub_secrets((decision.get("because") or cand.get("because", "")).strip())
    domain = cand.get("domain", "")
    tags = cand.get("tags", [])
    trigger = cand.get("trigger", "")
    touched = touched if touched is not None else []

    def _touch(eid: str | None) -> None:
        if eid:
            touched.append(eid)

    if action in ("UPDATE", "MERGE") and target_id:
        enzyme = await engine.get_enzyme(target_id)
        if not enzyme:                       # 目标没了 → 退化成新增
            res = await engine.harvest(insight=then, context=trigger, domain=domain, tags=tags)
            _touch(res.get("id") if isinstance(res, dict) else None)
            _count_new(summary, res)
            return
        prev_then, prev_because = enzyme.then, enzyme.because
        final, mode = _merge_then(enzyme.then, then)
        cap = settings.get("learning.update_max_chars")
        if cap > 0 and len(final) > cap and len(final) > len(enzyme.then):
            # 越塞越长就到此为止(实测长到 4601 字、判官只看前 400 字):
            # 候选自带完整正文,另存为一条新记忆,旧记忆保持原样
            res = await engine.harvest(insight=cand["then"].strip(), context=trigger,
                                       domain=domain, tags=tags)
            _touch(res.get("id") if isinstance(res, dict) else None)
            _count_new(summary, res)
            logger.info("capture UPDATE 增补超限(%d > %d),候选另存为新记忆", len(final), cap)
            return
        enzyme.then = final
        if because:
            enzyme.because = because
        enzyme.tags = list(dict.fromkeys([*enzyme.tags, *tags]))
        enzyme.confidence = min(1.0, enzyme.confidence + 0.05)
        enzyme.version += 1
        enzyme.updated_at = datetime.now(timezone.utc)
        await engine.update_enzyme(enzyme)
        await engine.pg.log_event(EvolutionEvent(
            enzyme_id=enzyme.id,
            event_type=EventType.CONSOLIDATION,
            detail={"action": "capture_update", "reason": decision.get("reason", ""),
                    "from_session": True,
                    "merge_mode": mode, "prev_then": prev_then, "prev_because": prev_because},
        ))
        _touch(enzyme.id)
        summary["updated"] += 1
        # 这次会话的情境本该想起这条记忆 → 记为经验样本(没被召回的也能从此学会被想起)
        await _learn_from_session(engine, enzyme, trigger, session_id, "capture_update")

    elif action == "REINFORCE" and target_id:
        res = await engine.hebbian_update(target_id, success=True, expected=True,
                                          detail="capture: 会话再次印证")
        if "error" not in res:
            _touch(target_id)
            target = await engine.get_enzyme(target_id)
            if target:
                await _learn_from_session(engine, target, trigger, session_id, "capture_reinforce")
        summary["reinforced" if "error" not in res else "errors"] += 1

    elif action == "INVALIDATE" and target_id:
        # 纠错性削弱旧酶(顺带触发负学习),再把新认知作为新酶加入
        await engine.hebbian_update(target_id, success=False, expected=True,
                                    detail=f"capture: 被新认知推翻 — {then}")
        res = await engine.harvest(insight=then, context=trigger, domain=domain, tags=tags)
        _touch(res.get("id") if isinstance(res, dict) else None)
        summary["invalidated"] += 1

    elif action == "ADD":
        res = await engine.harvest(insight=then, context=trigger, domain=domain, tags=tags)
        _touch(res.get("id") if isinstance(res, dict) else None)
        _count_new(summary, res)

    else:  # SKIP / 未知
        summary["skipped"] += 1


# ── 被动闭环:评估注入酶的实际效果 ──────────────────────────────

async def evaluate_injections(
    engine: Engine,
    transcript: str,
    injected_ids: list[str],
    session_id: str,
    summary: dict,
) -> None:
    """根据会话记录评估被 prime 注入的酶是否真的帮上了忙,落成 outcome 信号。

    这是学习环 outcome 侧的被动通道:不依赖 agent 自愿调 report,由 capture
    顺带完成。原则:
    - helped / contradicted 必须有对话内可指认的证据,否则一律 neutral;
    - neutral **不惩罚** —— 注入了没被用是匹配问题,归激活信号与 decay 管;
    - 以 (session_id, enzyme_id) 幂等,PreCompact 多次触发不会重复计分。
    """
    if not injected_ids:
        return
    pending = injected_ids
    if session_id:
        try:
            pending = await engine.pg.filter_unevaluated(session_id, injected_ids)
        except Exception:
            logger.debug("filter_unevaluated failed (non-fatal)", exc_info=True)
    enzymes = []
    for eid in pending[:_MAX_EVAL]:
        try:
            e = await engine.get_enzyme(eid)
        except Exception:
            e = None
        if e:
            enzymes.append(e)
    if not enzymes:
        return

    rules = "\n".join(
        f"- id={e.id} | 建议: {e.then} | 依据: {e.because}" for e in enzymes
    )
    prompt = f"""以下「直觉规则」曾在本次 AI 编程会话中被自动注入(推送给 agent 参考)。
请根据对话记录逐条评估其实际效果。

规则清单:
{rules}

对话记录:
\"\"\"
{clip_transcript(transcript)}
\"\"\"

判定标准(必须能在对话记录中指认证据;证据不足一律 neutral,不要臆断):
- "helped"        建议被明确采纳/遵循,且看得出正向效果或用户认可
- "contradicted"  建议被用户否定、被证明是错的、或照做后出了问题
- "neutral"       没被采纳、看不出影响、或证据不足

只返回 JSON 数组,每条规则一个元素:
[{{"id": "规则id", "verdict": "helped|contradicted|neutral", "evidence": "对话中的一句原文证据(neutral 可为空)"}}]"""

    result = await chat_json(
        system="你负责冷静评估记忆注入的实际效果,只依据对话中的证据下结论。只返回 JSON 数组。",
        user=prompt,
    )
    if not isinstance(result, list):
        return

    known = {e.id for e in enzymes}
    for item in result:
        if not isinstance(item, dict):
            continue
        eid = item.get("id")
        if eid not in known:
            continue
        verdict = (item.get("verdict") or "neutral").strip().lower()
        if verdict not in ("helped", "contradicted", "neutral"):
            verdict = "neutral"
        evidence = (item.get("evidence") or "").strip()[:200]
        try:
            res = None
            if verdict == "helped":
                res = await engine.hebbian_update(eid, success=True, expected=True,
                                                  detail=f"passive: {evidence}")
                summary["passive_helped"] += 1
            elif verdict == "contradicted":
                await engine.hebbian_update(eid, success=False, expected=True,
                                            detail=f"passive: {evidence}")
                summary["passive_contradicted"] += 1
            else:
                summary["passive_neutral"] += 1
            if session_id:
                await engine.pg.record_eval(session_id, eid, verdict)
                # 注入时的情境已不在进程内缓存(重启 / 超过 6 小时)→ 从会话的 prime 情境原文里挑
                if isinstance(res, dict) and "error" not in res and not res.get("learned_exemplar"):
                    await _learn_from_session(engine, next(e for e in enzymes if e.id == eid),
                                              "", session_id, "passive_helped", reveal=False)
            record = getattr(engine, "record_outcome", None)
            if record is not None:
                await record(eid, verdict, "passive", session_id=session_id,
                             decision_id=engine.last_decision(eid) if hasattr(engine, "last_decision") else "")
        except Exception:
            logger.exception("passive eval apply failed (non-fatal)")


# ── 编排入口 ───────────────────────────────────────────────────

async def capture_session(
    engine: Engine,
    transcript: str,
    domain: str = "",
    min_salience: float = 0.55,
    session_id: str = "",
    project: str = "",
) -> dict:
    """从一次会话 transcript 提取记忆并对账写入。返回处置统计。

    project 非空时给每个候选打 project:<slug> 标签 —— 新生酶记项目出身
    (召回时同项目加权),被 UPDATE 的酶合并标签后成为多项目酶。
    """
    summary = {"extracted": 0, "added": 0, "updated": 0, "reinforced": 0,
               "invalidated": 0, "skipped": 0, "known": 0, "errors": 0,
               "passive_helped": 0, "passive_contradicted": 0, "passive_neutral": 0}

    # 隐私脱敏:凭证/密钥绝不进入提炼与酶库
    transcript = scrub_secrets(transcript or "")
    # 剥离 prime 注入痕迹(防回声自强化),同时收集注入过的酶 id
    transcript, injected_ids = split_injections(transcript)

    candidates = await extract_candidates(transcript, domain)
    if project:
        ptag = f"project:{project}"
        for cand in candidates:
            tags = cand.get("tags") or []
            if ptag not in tags:
                cand["tags"] = [*tags, ptag]
    summary["extracted"] = len(candidates)
    touched: list[str] = []   # 本次会话提炼/强化/新增过的酶 → 情景卡的关联对象

    for cand in candidates[:_MAX_CANDIDATES]:
        try:
            if cand.get("salience", 0) < min_salience:
                summary["skipped"] += 1
                continue

            hits = await _find_similar(engine, cand)
            if not hits:
                # 确定性预过滤:没撞到相似酶 → 直接 ADD(走 harvest 的已会过滤+质量门)
                res = await engine.harvest(insight=cand["then"], context=cand.get("trigger", ""),
                                           domain=cand.get("domain", domain), tags=cand.get("tags", []))
                if isinstance(res, dict) and res.get("id"):
                    touched.append(res["id"])
                _count_new(summary, res)
                continue

            decision = await reconcile(cand, hits)
            if not decision:
                summary["errors"] += 1
                continue
            await _apply(engine, cand, decision, summary, touched, session_id=session_id)
        except Exception:
            logger.exception("capture candidate failed (non-fatal)")
            summary["errors"] += 1

    # 情景层:把这一幕落成情景卡,并给触达过的酶回填出处指针(best-effort)
    try:
        episode_id = await _write_episode(engine, transcript, domain, session_id, touched)
        if episode_id:
            summary["episode"] = episode_id
    except Exception:
        logger.exception("episode write failed (non-fatal)")

    # 被动闭环:评估注入酶的效果(best-effort,不影响主流程)
    try:
        await evaluate_injections(engine, transcript, injected_ids, session_id, summary)
    except Exception:
        logger.exception("passive injection eval failed (non-fatal)")

    logger.info("capture_session done: %s", summary)
    return summary


# ── 情景层写入 ─────────────────────────────────────────────────

async def _write_episode(
    engine: Engine,
    transcript: str,
    domain: str,
    session_id: str,
    touched: list[str],
) -> str | None:
    """把本次会话浓缩成情景卡入库,酶侧回填 episode: 出处指针。返回情景 id。"""
    from app.learning.episodes import card_to_episode, extract_episode

    card = await extract_episode(transcript, domain)
    if not card:
        return None
    episode = card_to_episode(card, session_id=session_id, enzyme_ids=touched)
    await engine.pg.upsert_episode(episode)

    # 出处回填:注入时 formatter 会把它渲染成 provenance("上次…"),
    # 让建议从"意见"变成"证词"。轻量列更新,不触发向量重嵌。
    date = episode.occurred_at.strftime("%Y-%m-%d")
    ref = f"episode:{episode.id}|{date}|{episode.title[:40]}"
    for eid in episode.enzyme_ids[:8]:
        try:
            await engine.pg.set_episode_ref(eid, ref)
        except Exception:
            logger.debug("set_episode_ref failed (non-fatal)", exc_info=True)
    return episode.id
