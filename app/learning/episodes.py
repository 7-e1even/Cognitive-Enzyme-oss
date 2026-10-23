"""Episodes — 情景记忆提取与线索归一化(情景层写路径)。

capture 提炼酶(语义规则,"该做什么")的同时,由本模块把整段会话浓缩成一张
情景卡(episodic trace,"当时发生了什么"):骨架 + 原文摘录 + 回想线索(cues)。

方法论对应:
- 编码特异性:回想的上限在写入时刻决定,所以 entities/error_signatures/excerpt
  存的是"当时的样子"(摘录),不是总结。
- 模式补全:cues 是归一化的线索集,回想时按残缺线索与它做重叠评分。
"""

from __future__ import annotations

import logging
import re

from app.core import Episode
from app.learning.transcript import clip_transcript
from app.llm_client import chat_json

logger = logging.getLogger(__name__)

_MAX_CUES = 24
_MAX_EXCERPT = 1500
_MIN_SALIENCE = 0.4   # 低于此的会话(闲聊/琐碎)不值得存一幕


# ── 线索归一化 ─────────────────────────────────────────────────

_TERM_RES = [
    re.compile(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+"),          # CamelCase
    re.compile(r"[a-zA-Z_]+\.[a-zA-Z_]+(?:\.[a-zA-Z_]+)*"),  # 点分标识符/文件名
    re.compile(r"\b[A-Z]{3,}\b"),                        # 全大写缩略语
    re.compile(r"\b[A-Z][a-z]{3,}\b"),                   # 首大写单词(Unauthorized)
    re.compile(r"\b\d{3}\b"),                            # 三位错误码(401/500)
    re.compile(r"[一-鿿]{2,6}"),                 # 中文术语
    re.compile(r"\b[a-z][a-z0-9_-]{3,}\b"),              # 普通小写标识符
]


def cues_from_text(text: str, limit: int = _MAX_CUES) -> list[str]:
    """从自由文本抽线索词(小写归一、保序去重)。回想入口无显式 cues 时也用它。"""
    found: list[str] = []
    for pattern in _TERM_RES:
        found += pattern.findall(text or "")
    seen: set[str] = set()
    out: list[str] = []
    for term in found:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out[:limit]


def build_cues(card: dict) -> list[str]:
    """情景卡 → 归一化线索集。

    实体和报错签名是最稳定的编码线索;事件链与结局补充"当时发生了什么"
    的检索信号,最后再从场景骨架提取通用词。这样只记得某个事件动作时,
    也能回想到对应场景,同时保留原有的实体/错误码召回行为。
    """
    explicit: list[str] = []
    for field in ("entities", "error_signatures"):
        for item in card.get(field) or []:
            explicit += cues_from_text(str(item), limit=4) or [str(item).strip().lower()]
    # 事件链是场景回想的核心线索。拆开处理,避免一整段 transcript 互相吞掉
    # 事件之间的边界;每个事件最多贡献少量归一化词。
    event_cues: list[str] = []
    for item in card.get("events") or []:
        event_cues += cues_from_text(str(item), limit=4)
    skeleton = " ".join(
        str(card.get(f, "")) for f in ("title", "intent", "root_cause", "outcome", "lesson")
    )
    # 保证一张事件丰富的卡不会用实体/报错词把事件线索挤出 24 项上限;
    # 仍给骨架留出空间,兼容旧卡的标题/教训召回。
    merged = explicit[:12] + event_cues[:8] + cues_from_text(skeleton)
    seen: set[str] = set()
    out: list[str] = []
    for cue in merged:
        if cue and cue not in seen:
            seen.add(cue)
            out.append(cue)
    return out[:_MAX_CUES]


# ── 情景卡提取 ─────────────────────────────────────────────────

async def extract_episode(transcript: str, domain: str = "") -> dict | None:
    """从 transcript 提炼一张情景卡(dict,未落库)。琐碎会话返回 None。"""
    transcript = (transcript or "").strip()
    if not transcript:
        return None

    prompt = f"""下面是一次 AI 编程会话的对话记录。请把它浓缩成一张「情景卡」——
记录这一幕**发生了什么**,供未来"回想起当时"使用。注意:excerpt 必须是对话中的
**原文摘录**(报错原文、关键对话原句),不是你的总结;其余字段才是概括。

对话记录:
\"\"\"
{clip_transcript(transcript)}
\"\"\"

只返回一个 JSON 对象:
{{
  "title": "一句话:这一幕是什么(如'修复 JWT 刷新令牌死循环')",
  "intent": "当时想做什么",
  "events": ["关键事件按时间序,3-6条,每条一句话"],
  "root_cause": "根因(没有则空串)",
  "outcome": "结局:解决了/绕过了/未解决,以及最终状态一句话",
  "lesson": "带走的教训一句话(没有则空串)",
  "entities": ["涉及的文件/函数/API/组件/工具名,原样"],
  "error_signatures": ["报错的关键行原文,最多3条,没有则空数组"],
  "excerpt": "对话中最关键的原文片段摘录(≤800字)",
  "salience": 0.0到1.0,这一幕未来被回想起的价值(纯闲聊/琐碎操作给低分)
}}

如果整段会话没有任何值得记住的一幕,返回 null。"""

    result = await chat_json(
        system="你负责把工程会话浓缩成可回想的情景记忆,摘录忠实、概括克制。只返回 JSON 对象或 null。",
        user=prompt,
    )
    if not isinstance(result, dict):
        return None
    title = (result.get("title") or "").strip()
    if not title:
        return None
    try:
        salience = float(result.get("salience", 0.5) or 0.0)
    except (TypeError, ValueError):
        salience = 0.5
    if salience < _MIN_SALIENCE:
        return None
    return {
        "title": title,
        "intent": (result.get("intent") or "").strip(),
        "events": [str(e).strip() for e in (result.get("events") or []) if str(e).strip()][:6],
        "root_cause": (result.get("root_cause") or "").strip(),
        "outcome": (result.get("outcome") or "").strip(),
        "lesson": (result.get("lesson") or "").strip(),
        "entities": [str(e).strip() for e in (result.get("entities") or []) if str(e).strip()][:12],
        "error_signatures": [str(e).strip() for e in (result.get("error_signatures") or []) if str(e).strip()][:3],
        "excerpt": (result.get("excerpt") or "").strip()[:_MAX_EXCERPT],
        "salience": max(0.0, min(1.0, salience)),
        "domain": domain,
    }


def card_to_episode(
    card: dict,
    session_id: str = "",
    enzyme_ids: list[str] | None = None,
) -> Episode:
    """情景卡 dict → Episode 模型(补 cues 与关联酶)。"""
    return Episode(
        session_id=session_id,
        domain=card.get("domain", ""),
        title=card["title"],
        intent=card.get("intent", ""),
        events=card.get("events", []),
        root_cause=card.get("root_cause", ""),
        outcome=card.get("outcome", ""),
        lesson=card.get("lesson", ""),
        entities=card.get("entities", []),
        error_signatures=card.get("error_signatures", []),
        cues=build_cues(card),
        excerpt=card.get("excerpt", ""),
        enzyme_ids=list(dict.fromkeys(enzyme_ids or [])),
        salience=card.get("salience", 0.5),
    )
