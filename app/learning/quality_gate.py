"""Quality Gate — LLM-based enzyme quality assessment before storage.

Evaluates harvested insights on 5 dimensions, returns a decision:
  pass / refine / quarantine / reject
LLM 调不通时先退回判官(Jev)的 score 题打分,判官也不可用才用规则检查。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────

PASS = "pass"
REFINE = "refine"
QUARANTINE = "quarantine"
REJECT = "reject"

DIMENSIONS = ["specificity", "novelty", "accuracy", "universality", "actionability"]


@dataclass
class QualityResult:
    decision: str              # pass / refine / quarantine / reject
    score: float               # 0.0 - 5.0 average
    dimensions: dict[str, float] = field(default_factory=dict)
    suggestions: list[str] = field(default_factory=list)
    improved_insight: str = ""  # LLM-refined version (when decision=refine)
    reason: str = ""


# ── Thresholds ────────────────────────────────────────────────

PASS_THRESHOLD = 3.5
REFINE_THRESHOLD = 2.5
QUARANTINE_THRESHOLD = 2.0


# ── LLM Assessment ───────────────────────────────────────────

async def assess_quality(
    insight: str,
    context: str,
    similar_enzymes: list[dict] | None = None,
    typesafe_config=None,
) -> QualityResult:
    """Assess enzyme quality: LLM → judge (Jev score questions) → rules."""

    # Try LLM first
    result = await _llm_assess(insight, context, similar_enzymes)
    if result:
        return result

    if typesafe_config is not None:
        result = await _jev_assess(insight, context, similar_enzymes, typesafe_config)
        if result:
            return result

    # Fallback to rules
    return _rule_assess(insight, context, similar_enzymes)


async def _llm_assess(
    insight: str,
    context: str,
    similar_enzymes: list[dict] | None = None,
) -> QualityResult | None:
    """Use LLM to evaluate enzyme quality on 5 dimensions."""
    from app.llm_client import chat_json

    similar_text = ""
    if similar_enzymes:
        items = [f"- {e.get('then', '')[:80]}" for e in similar_enzymes[:5]]
        similar_text = f"\n\n已有相似酶:\n" + "\n".join(items)

    prompt = f"""你是认知酶质量审核专家。评估以下经验是否值得作为"认知酶"存入直觉库。

待评估经验:
  insight: {insight}
  context: {context[:300]}
{similar_text}

请从 5 个维度打分 (1-5):
1. specificity: 是否足够具体？(1=太模糊如"注意安全", 5=精确如"Spring Boot 2.6+Thymeleaf检查SSTI")
2. novelty: 是否有新信息？和已有酶重复吗？(1=完全重复, 5=全新洞察)
3. accuracy: 事实是否正确？(1=明显错误, 5=准确可靠)
4. universality: 是否可跨场景复用？(1=只适用单一场景, 5=通用模式)
5. actionability: 建议是否可执行？(1="可能有问题", 5=给出具体检查步骤)

返回 JSON:
{{
  "scores": {{"specificity": N, "novelty": N, "accuracy": N, "universality": N, "actionability": N}},
  "suggestions": ["改进建议1", ...],
  "improved_insight": "如果分数<3.5，给出改进后的版本；否则留空",
  "reason": "一句话总结评估理由"
}}"""

    result = await chat_json(
        system="你是认知酶质量审核专家。严格评估，只返回 JSON。",
        user=prompt,
    )

    if not result or not isinstance(result, dict):
        return None

    scores = result.get("scores", {})
    if not scores:
        return None

    dims = {}
    for d in DIMENSIONS:
        val = scores.get(d)
        if isinstance(val, (int, float)):
            dims[d] = max(1.0, min(5.0, float(val)))

    if not dims:
        return None

    avg = sum(dims.values()) / len(dims)
    suggestions = result.get("suggestions", [])
    improved = result.get("improved_insight", "")
    reason = result.get("reason", "")

    decision = _decide(avg, improved)

    logger.info(
        "Quality gate: score=%.2f decision=%s reason=%s",
        avg, decision, reason[:60],
    )

    return QualityResult(
        decision=decision,
        score=round(avg, 2),
        dimensions=dims,
        suggestions=suggestions,
        improved_insight=improved,
        reason=reason,
    )


# ── Judge Fallback(Jev score 题)────────────────────────────
# 与上面 LLM 提示词同一套标准:两端锚点照搬,中间档补齐;每维 5 档,返回的 0~4 档 → 1~5 分。
# 测试平台 33 条真实候选上与 GLM 的决定一致 28/33(GLM 自己两遍 33/33):偏宽松、事实准确性判断较弱,
# 所以只做 LLM 失败时的兜底——比按长度 / 关键词的规则可靠得多。只打分不写改写稿,不会给出 refine。

_JUDGE_RUBRIC = {
    "specificity": ("insight 是否足够具体?",
                    ["太模糊,如「注意安全」", "偏模糊,缺少对象或条件", "有明确对象,但缺少条件或做法",
                     "较具体,指明了对象和做法", "精确,如「Spring Boot 2.6+Thymeleaf 检查 SSTI」"]),
    "novelty": ("和「已有相似记忆」相比,insight 是否有新信息?",
                ["和已有记忆完全重复", "大部分重复", "有少量新信息", "大部分是新信息", "全新洞察"]),
    "accuracy": ("insight 的事实是否正确?", ["明显错误", "可能有错", "部分正确", "基本准确", "准确可靠"]),
    "universality": ("insight 是否可跨场景复用?",
                     ["只适用单一场景", "适用面很窄", "适用于一类相近场景", "适用于多类场景", "通用模式"]),
    "actionability": ("insight 的建议是否可执行?",
                      ["只说「可能有问题」", "方向模糊", "有做法但缺步骤", "给出了做法", "给出具体检查步骤"]),
}


async def _jev_assess(
    insight: str,
    context: str,
    similar_enzymes: list[dict] | None,
    config,
) -> QualityResult | None:
    from app.typesafe_client import system_one

    # 判官单次请求有 token 硬上限:insight 截到 1200 字(context 300 / 相似项 80 本来就有截)
    state = {"insight": insight[:1200], "context": context[:300],
             "已有相似记忆": [e.get("then", "")[:80] for e in (similar_enzymes or [])[:5]]}
    questions = {d: {"type": "score", "instructions": q, "criteria": levels}
                 for d, (q, levels) in _JUDGE_RUBRIC.items()}
    data = await system_one(state, questions, config, timeout=15.0)
    if not data:
        return None
    try:
        dims = {d: max(1.0, min(5.0, float(data["answers"][d]["score"]) + 1)) for d in _JUDGE_RUBRIC}
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Quality gate judge answers malformed: %s", e)
        return None
    avg = sum(dims.values()) / len(dims)
    decision = _decide(avg, "")
    logger.info("Quality gate (judge fallback): score=%.2f decision=%s", avg, decision)
    return QualityResult(decision=decision, score=round(avg, 2), dimensions=dims,
                         reason="判官打分(语言模型不可用)")


# ── Rule-based Fallback ──────────────────────────────────────

def _rule_assess(
    insight: str,
    context: str,
    similar_enzymes: list[dict] | None = None,
) -> QualityResult:
    """Lightweight rule-based check when LLM is unavailable."""
    scores: dict[str, float] = {}
    suggestions: list[str] = []

    # specificity: by length and keyword density
    if len(insight) < 10:
        scores["specificity"] = 1.5
        suggestions.append("insight 太短，缺少具体细节")
    elif len(insight) < 30:
        scores["specificity"] = 2.5
    else:
        scores["specificity"] = 3.5

    # novelty: check against similar enzymes
    if similar_enzymes:
        # Simple text overlap check
        insight_lower = insight.lower()
        for enz in similar_enzymes:
            existing = enz.get("then", "").lower()
            if existing and (
                insight_lower in existing
                or existing in insight_lower
                or _jaccard_words(insight_lower, existing) > 0.6
            ):
                scores["novelty"] = 1.5
                suggestions.append(f"和已有酶高度相似: {existing[:50]}")
                break
        else:
            scores["novelty"] = 3.5
    else:
        scores["novelty"] = 3.0  # Can't verify, neutral

    # accuracy: can't verify without LLM, be generous
    scores["accuracy"] = 3.0

    # universality: check for overly specific markers
    specific_markers = ["我的", "我们的", "这个项目", "tmp/", "本地"]
    if any(m in insight for m in specific_markers):
        scores["universality"] = 2.0
        suggestions.append("包含过于具体的引用，不够通用")
    else:
        scores["universality"] = 3.0

    # actionability: check for action verbs
    action_verbs = ["检查", "验证", "尝试", "使用", "配置", "添加", "检测",
                     "check", "verify", "try", "use", "test", "inspect"]
    if any(v in insight.lower() for v in action_verbs):
        scores["actionability"] = 3.5
    else:
        scores["actionability"] = 2.5
        suggestions.append("建议包含具体的行动动词")

    avg = sum(scores.values()) / len(scores)
    decision = _decide(avg, "")

    return QualityResult(
        decision=decision,
        score=round(avg, 2),
        dimensions=scores,
        suggestions=suggestions,
        reason="规则检查（LLM 不可用）",
    )


# ── Helpers ───────────────────────────────────────────────────

def _decide(avg_score: float, improved_insight: str) -> str:
    if avg_score >= PASS_THRESHOLD:
        return PASS
    elif avg_score >= REFINE_THRESHOLD and improved_insight:
        return REFINE
    elif avg_score >= QUARANTINE_THRESHOLD:
        return QUARANTINE
    else:
        return REJECT


def _jaccard_words(a: str, b: str) -> float:
    """Word-level Jaccard similarity."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
