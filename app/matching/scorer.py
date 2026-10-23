"""Scorer — multi-dimensional enzyme relevance scoring.

Five dimensions (inspired by PlugMem's Value Function):
  - Relevance (35%): pattern/rerank match quality
  - Importance (25%): enzyme confidence
  - Return (20%): historical precision (success rate)
  - Recency (15%): time decay since last triggered
  - Credibility (5%): usage-based trust (triggered enough to be reliable)
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from app import settings
from app.core import Enzyme


def _weights() -> tuple[float, float, float, float, float]:
    """(相关性, 重要性, 回报, 新近度, 可信度) 权重,运行参数 ranking.w_*。
    评测 knobC: 相关性权重上调 0.35→0.55(recall +1pt, precision +12.7pt,
    静默率 0%→100%, MRR +0.088;无指标恶化)。"""
    return (settings.get("ranking.w_relevance"), settings.get("ranking.w_importance"),
            settings.get("ranking.w_return"), settings.get("ranking.w_recency"),
            settings.get("ranking.w_credibility"))


def _recency_score(enzyme: Enzyme) -> float:
    """Compute recency score based on last use.

    "Last use" is the more recent of last_triggered (reported) and
    last_activated (matched by prime()), so a freshly-matched enzyme ranks as
    fresh even before any report() arrives.

    Returns 1.0 for recently used (within 7 days),
    decays exponentially to ~0.1 at 180 days,
    and floors at 0.1 for never-used enzymes.
    """
    last_used = max(
        [t for t in (enzyme.last_triggered, enzyme.last_activated) if t is not None],
        default=None,
    )
    if not last_used:
        return 0.1

    age_days = (datetime.now(timezone.utc) - last_used).total_seconds() / 86400
    if age_days <= 0:
        return 1.0
    # Exponential decay: half-life ~30 days
    return max(0.1, math.exp(-0.023 * age_days))


def compute_score(
    enzyme: Enzyme,
    pattern_score: float,
    rel_boost: float = 0.0,
    urgency: float = 0.5,
) -> float:
    """Compute final relevance score using multi-dimensional value function.

    Combines five dimensions into a weighted score, plus relation boost:
        - pattern_score: how well context matches (0-1, from pattern/rerank)
        - enzyme attributes: confidence, precision, trigger_count, last_triggered
        - rel_boost: bonus from graph relations (0+, uncapped)
        - urgency: gain modulation (0-1), derived from candidate severity distribution.
          High urgency → amplifies importance (only proven enzymes surface).
          Low urgency  → dampens importance (exploratory enzymes get a chance).
    """
    # Gain modulation: urgency 0→1 maps gain 0.5→1.5
    gain = 0.5 + urgency

    w_rel, w_imp, w_ret, w_rec, w_cred = _weights()
    relevance   = pattern_score * w_rel
    importance  = enzyme.confidence * w_imp * gain
    return_     = enzyme.precision * w_ret
    recency     = _recency_score(enzyme) * w_rec
    credibility = min(enzyme.trigger_count / 20, 1.0) * w_cred

    return relevance + importance + return_ + recency + credibility + rel_boost


def conditions_mode() -> str:
    """CE_CONDITIONS_MODE:

    - "soft"(默认): conditions 不淘汰候选,只在命中时给 pattern_score 小幅加成
      (见 pipeline),仍参与排序区分度。negations 两种模式下都照旧过滤——
      它是明确排除信号,不是"缺失证据"。
    - "hard": 全部 conditions 必须精确子串命中,否则整条淘汰(旧行为)。
    评测 knobD2(round-1 knobD 已测量 11/11 抽样条件杀全为误杀——跨语言
    query/condition 不匹配或 AND 语义过严,设计规范授权放宽):soft vs hard,
    recall 74.0%→82.0%(+8pt), precision 38.2%→42.5%(+4.3pt), mrr +0.032,
    静默率持平 100%,killed_conditions 11→0,无指标恶化 → soft 定为默认。
    非法值回退 "soft"。
    """
    return settings.get("ranking.conditions_mode")


def check_conditions(enzyme: Enzyme, context: str) -> bool:
    """Check if enzyme's conditions are satisfied and negations are absent.

    hard 模式:全部 conditions 命中 + 无 negation 命中才通过。
    soft 模式:conditions 不参与淘汰(只影响打分,见 pipeline),仅 negation 淘汰。
    """
    context_lower = context.lower()

    if conditions_mode() == "hard":
        for cond in enzyme.conditions:
            if cond.lower() not in context_lower:
                return False

    # No negation should be present (both modes)
    for neg in enzyme.negations:
        if neg.lower() in context_lower:
            return False

    return True
