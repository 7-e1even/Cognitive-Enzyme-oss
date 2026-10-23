"""Replay — 情景回放测试(离线的「应用/测试」阶段)。

酶被写入后,只有等真实会话恰好再遇到相同情境才有验证机会——探索槽假设
和低反馈酶因此长期停在"未考过试"的状态。回放把已存的情景卡当考题:
用"事发时刻已知的情境"重跑匹配管线,再让 LLM 以"事后已知的结局"为
标准答案给命中的酶判卷,把历史经验变成廉价、可重复的离线验证信号。

设计不变量:
- **防自证**:凡从该幕提炼出的酶(episode.enzyme_ids,或 references 带
  episode:{id} 出处指针)一律不判——用自己的出处考自己必然满分。
- **考题不含答案**:考卷只用 title/intent/events/entities/error_signatures;
  root_cause/outcome/lesson/excerpt 只进判卷标准。
- **弱信号**:计分走 hebbian_update(credit=0.5)——回放判断弱于真实反馈,
  置信度增量减半;统计照记(让 precision 有数据正是回放的目的)。
  neutral 不惩罚(与被动评估同理,没匹配上归激活信号与 decay 管)。
- **不污染使用信号**:直接调 engine.match,绝不 record_activations /
  不应期 / co-trigger——回放不是使用。
- **幂等**:replay_evals(episode_id, enzyme_id) 主键,判过即终局
  (情景是冻结的,重判无新信息;这点与 capture_evals 的 neutral 可升级不同)。
"""

from __future__ import annotations

import logging

from app.core import Enzyme, Episode, EventType, EvolutionEvent
from app.core.engine import Engine
from app.llm_client import chat_json

logger = logging.getLogger(__name__)

_MAX_EPISODES = 5      # 每轮巩固最多回放的幕数(控成本)
_MAX_JUDGE = 8         # 每幕最多判卷的酶数
_MATCH_LIMIT = 8       # 考试时的匹配召回上限
_CREDIT = 0.5          # 回放信号的信用系数(弱于真实/被动反馈)


# ── 考卷与防自证 ───────────────────────────────────────────────

def exam_context(episode: Episode) -> str:
    """情景 → 考题:只还原"事发时刻已知的信息",结局字段绝不入卷。"""
    parts: list[str] = [episode.title, episode.intent]
    parts += episode.events
    if episode.entities:
        parts.append(" ".join(episode.entities))
    parts += episode.error_signatures
    return "\n".join(p.strip() for p in parts if p and p.strip())


def is_self_sourced(enzyme: Enzyme, episode: Episode) -> bool:
    """该酶是否源自这一幕(提炼关联或出处指针)——是则不许自考。"""
    if enzyme.id in episode.enzyme_ids:
        return True
    tag = f"episode:{episode.id}"
    return any(str(r).startswith(tag) for r in enzyme.references)


# ── 判卷 ───────────────────────────────────────────────────────

async def judge(episode: Episode, enzymes: list[Enzyme]) -> list[dict]:
    """以已知结局为标准答案,逐条裁决规则"当时会不会有帮助"。"""
    rules = "\n".join(
        f"- id={e.id} | 建议: {e.then} | 依据: {e.because}" for e in enzymes
    )
    events = "\n".join(f"  - {ev}" for ev in episode.events) or "  (无)"
    errors = "\n".join(f"  - {sig}" for sig in episode.error_signatures) or "  (无)"
    prompt = f"""以下是一幕过去真实发生过的工程情景(结局已知),以及若干条「直觉规则」。
请假设事发时刻这些规则被推送给了工程师,以事后已知的结局为标准答案,
逐条判断该规则当时会不会有帮助。

情景 · 事发时刻已知的信息:
- 标题: {episode.title}
- 意图: {episode.intent}
- 经过:
{events}
- 报错:
{errors}

情景 · 事后已知的结局(标准答案,仅供判卷):
- 根因: {episode.root_cause or "(未记录)"}
- 结局: {episode.outcome or "(未记录)"}
- 教训: {episode.lesson or "(未记录)"}
- 原文摘录: {episode.excerpt[:600] or "(无)"}

规则清单:
{rules}

判定标准(依据必须落在这一幕的信息之内;不确定一律 neutral,不要臆断):
- "helped"        规则的建议与根因/教训一致,照做会更快解决或避开这个坑
- "contradicted"  规则的建议与这一幕证明的事实相悖,照做会被误导
- "neutral"       与这一幕无关、影响不明、或证据不足

只返回 JSON 数组,每条规则一个元素:
[{{"id": "规则id", "verdict": "helped|contradicted|neutral", "evidence": "一句依据(neutral 可为空)"}}]"""

    result = await chat_json(
        system="你负责用已知结局的历史情景冷静检验直觉规则,只依据这一幕内的证据下结论。只返回 JSON 数组。",
        user=prompt,
    )
    return result if isinstance(result, list) else []


# ── 单幕回放 ───────────────────────────────────────────────────

async def replay_episode(engine: Engine, episode: Episode, summary: dict) -> None:
    """一幕 = 一场考试:匹配 → 防自证过滤 → 幂等过滤 → 判卷 → 记分。"""
    context = exam_context(episode)
    if not context:
        summary["skipped_empty"] += 1
        return

    results = await engine.match(
        context, domain=episode.domain or None, max_results=_MATCH_LIMIT, source="replay",
    )
    candidates = [r.enzyme for r in results if not is_self_sourced(r.enzyme, episode)]
    summary["self_excluded"] += len(results) - len(candidates)
    if not candidates:
        return

    pending = set(await engine.pg.filter_unreplayed(
        episode.id, [e.id for e in candidates],
    ))
    enzymes = [e for e in candidates if e.id in pending][:_MAX_JUDGE]
    if not enzymes:
        return

    verdicts = await judge(episode, enzymes)
    known = {e.id for e in enzymes}
    for item in verdicts:
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
            if verdict == "helped":
                await engine.hebbian_update(eid, success=True, expected=True,
                                            detail=f"replay: {evidence}", credit=_CREDIT)
                summary["helped"] += 1
            elif verdict == "contradicted":
                await engine.hebbian_update(eid, success=False, expected=True,
                                            detail=f"replay: {evidence}", credit=_CREDIT)
                summary["contradicted"] += 1
            else:
                summary["neutral"] += 1
            await engine.pg.record_replay_eval(episode.id, eid, verdict)
            await engine.pg.log_event(EvolutionEvent(
                enzyme_id=eid,
                event_type=EventType.REPLAY,
                detail={"episode_id": episode.id, "verdict": verdict,
                        "evidence": evidence, "credit": _CREDIT},
            ))
        except Exception:
            logger.exception("replay verdict apply failed (non-fatal)")
            summary["errors"] += 1


# ── 编排入口(巩固管线的一步) ──────────────────────────────────

async def run_replay_cycle(engine: Engine, max_episodes: int = _MAX_EPISODES) -> dict:
    """取一批情景做回放考试。任何一幕失败不影响其余。"""
    summary = {"episodes": 0, "helped": 0, "contradicted": 0, "neutral": 0,
               "self_excluded": 0, "skipped_empty": 0, "errors": 0}
    try:
        episodes = await engine.pg.replay_candidates(limit=max_episodes)
    except Exception:
        logger.exception("replay candidate query failed")
        summary["errors"] += 1
        return summary

    for episode in episodes:
        summary["episodes"] += 1
        try:
            await replay_episode(engine, episode, summary)
        except Exception:
            logger.exception("replay episode %s failed (non-fatal)", episode.id)
            summary["errors"] += 1

    logger.info("replay cycle done: %s", summary)
    return summary
