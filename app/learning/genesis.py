"""Genesis — generate new enzymes from unexpected outcomes.

LLM-enhanced: uses LLM to extract more nuanced patterns from free-text.
Falls back to self-bootstrapping extraction from the enzyme database.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from app.core import Pattern, PatternType

if TYPE_CHECKING:
    from app.storage.pg_store import PGStore

logger = logging.getLogger(__name__)


async def extract_patterns_llm(text: str, max_patterns: int = 5) -> list[Pattern] | None:
    """Use LLM to extract security-relevant patterns from text."""
    from app.llm_client import chat_json

    prompt = f"""你是安全研究专家。从以下文本中提取可以用作安全检测规则匹配关键词的模式。

文本: {text[:500]}

请提取 3-{max_patterns} 个最有辨识度的关键词/模式。优先提取:
1. 具体的类名、方法名、函数名
2. CVE 编号
3. 工具名、框架名
4. 安全术语
5. 配置项、路径

返回 JSON 数组: [{{"value": "关键词", "type": "keyword"}}]
如果某个模式适合用正则匹配，type 填 "regex"。"""

    result = await chat_json(
        system="你是安全专家，提取技术关键词。只返回 JSON。",
        user=prompt,
    )

    if not result or not isinstance(result, list):
        return None

    patterns = []
    seen = set()
    for item in result:
        val = item.get("value", "").strip()
        ptype = item.get("type", "keyword")
        if val and val.lower() not in seen:
            pt = PatternType.REGEX if ptype == "regex" else PatternType.KEYWORD
            patterns.append(Pattern(type=pt, value=val, weight=1.2))
            seen.add(val.lower())

    if patterns:
        logger.info("LLM extracted %d patterns from text", len(patterns))
    return patterns[:max_patterns] if patterns else None


async def extract_patterns(
    text: str,
    pg: PGStore | None = None,
    max_patterns: int = 5,
) -> list[Pattern]:
    """Extract patterns from text using enzyme database + regex rules.

    Self-bootstrapping: uses the enzyme database's own pattern keywords
    as the domain term list, instead of a hardcoded list. The richer the
    enzyme database, the better the extraction — a virtuous cycle.

    Falls back to generic regex extraction if no DB is available.
    """
    patterns: list[Pattern] = []

    # 1. Self-bootstrapping: match against known terms from enzyme DB
    if pg:
        known_terms = await pg.get_known_terms(min_confidence=0.5)
        if known_terms:
            text_lower = text.lower()
            for term in known_terms:
                if term.lower() in text_lower:
                    patterns.append(Pattern(type=PatternType.KEYWORD, value=term, weight=1.5))

    # 2. CamelCase / dot-separated identifiers
    identifiers = re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)+', text)
    identifiers += re.findall(r'[a-zA-Z]+\.[a-zA-Z]+(?:\.[a-zA-Z]+)*', text)
    for ident in identifiers:
        if ident not in [p.value for p in patterns]:
            patterns.append(Pattern(type=PatternType.KEYWORD, value=ident))

    # 3. CVE patterns
    cves = re.findall(r'CVE-\d{4}-\d{4,}', text, re.IGNORECASE)
    for cve in cves:
        if cve not in [p.value for p in patterns]:
            patterns.append(Pattern(type=PatternType.KEYWORD, value=cve, weight=1.3))

    # 4. Fallback: significant words (>3 chars)
    if len(patterns) < 2:
        _STOP_WORDS = {"this", "that", "with", "from", "have", "been", "will", "they", "their",
                        "when", "what", "which", "there", "into", "some", "than", "then",
                        "only", "also", "more", "very", "most", "just", "about"}
        words = re.findall(r'\b[a-zA-Z\u4e00-\u9fff]{3,}\b', text)
        for w in words:
            if w.lower() not in _STOP_WORDS and w not in [p.value for p in patterns]:
                patterns.append(Pattern(type=PatternType.KEYWORD, value=w))

    # Deduplicate and limit
    seen = set()
    unique: list[Pattern] = []
    for p in patterns:
        key = p.value.lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique[:max_patterns]
