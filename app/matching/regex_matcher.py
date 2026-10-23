"""Regex matcher — regex pattern matching with weight scoring."""

from __future__ import annotations

import re
import logging

from app.core import Enzyme, PatternType

logger = logging.getLogger(__name__)


def regex_match(enzyme: Enzyme, text: str) -> float:
    """Check if text matches enzyme's regex patterns.

    Returns a score (0.0 = no match, higher = better match).
    """
    total_weight = 0.0
    matched_weight = 0.0

    for pattern in enzyme.patterns:
        if pattern.type != PatternType.REGEX:
            continue
        total_weight += pattern.weight
        try:
            if re.search(pattern.value, text, re.IGNORECASE | re.MULTILINE):
                matched_weight += pattern.weight
        except re.error:
            logger.warning("Invalid regex pattern: %s", pattern.value)

    if total_weight == 0:
        return 0.0
    return matched_weight / total_weight
