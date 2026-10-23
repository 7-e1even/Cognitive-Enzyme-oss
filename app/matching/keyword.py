"""Keyword matcher — exact keyword matching with weight scoring."""

from __future__ import annotations

from app.core import Enzyme, PatternType


def keyword_match(enzyme: Enzyme, text: str) -> float:
    """Check if text contains enzyme's keyword patterns.

    Returns a score (0.0 = no match, higher = better match).
    """
    text_lower = text.lower()
    total_weight = 0.0
    matched_weight = 0.0

    for pattern in enzyme.patterns:
        if pattern.type != PatternType.KEYWORD:
            continue
        total_weight += pattern.weight
        if pattern.value.lower() in text_lower:
            matched_weight += pattern.weight

    if total_weight == 0:
        return 0.0
    return matched_weight / total_weight
