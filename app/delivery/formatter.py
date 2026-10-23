"""Formatter — format enzyme match results for output."""

from __future__ import annotations

from app.core import Enzyme, Episode
from app.matching.pipeline import MatchResult


def _split_provenance(references: list[str]) -> tuple[str | None, list[str]]:
    """从 references 里剥出 episode: 出处指针,渲染成人话。

    capture 写入格式: ``episode:<id>|<date>|<title>``。出处让建议从"意见"
    变成"证词"("上次在这幕里验证过"),单独成字段,不占 references 名额。
    """
    provenance = None
    rest = []
    for ref in references:
        if isinstance(ref, str) and ref.startswith("episode:"):
            parts = ref[len("episode:"):].split("|", 2)
            if len(parts) == 3:
                ep_id, date, title = parts
                provenance = f"{date} {title} (episode:{ep_id})"
            else:
                provenance = ref
        else:
            rest.append(ref)
    return provenance, rest


def _scope(r: MatchResult) -> str:
    """Expose the storage scope so hooks can distinguish hints from facts."""
    return "general" if "scope:general" in r.enzyme.tags else "specific"


def format_match_results(results: list[MatchResult]) -> list[dict]:
    """Full variant with ranking artifacts (tier/score) — for debugging/inspection UIs."""
    output = []
    for r in results:
        item = {
            "id": r.enzyme.id,
            "tier": r.tier,
            "score": round(r.score, 3),
            "suggestion": r.enzyme.then,
            "severity": r.enzyme.severity.value,
            "confidence": round(r.enzyme.confidence, 2),
            "scope": _scope(r),
        }
        if r.exploratory:
            item["exploratory"] = True
        if r.enzyme.because:
            item["reason"] = r.enzyme.because
        provenance, refs = _split_provenance(r.enzyme.references)
        if provenance:
            item["provenance"] = provenance
        if refs:
            item["references"] = refs[:3]
        output.append(item)
    return output


def format_match_results_slim(results: list[MatchResult]) -> list[dict]:
    """Token-lean variant for model-facing prime paths (MCP prime() 与 REST /api/prime).

    Drops internal ranking artifacts (``tier``/``score``) the LLM never acts on,
    keeping only what's needed to apply the intuition and report() back.
    """
    output = []
    for r in results:
        item = {
            "id": r.enzyme.id,
            "suggestion": r.enzyme.then,
            "severity": r.enzyme.severity.value,
            "confidence": round(r.enzyme.confidence, 2),
            "scope": _scope(r),
        }
        if r.exploratory:
            item["exploratory"] = True   # 探索槽假设:hook 端豁免置信度过滤并标注
        if r.enzyme.because:
            item["reason"] = r.enzyme.because
        provenance, refs = _split_provenance(r.enzyme.references)
        if provenance:
            item["provenance"] = provenance   # 出处一行,可用 recall 钻取整幕
        if refs:
            item["references"] = refs[:2]
        output.append(item)
    return output


# ── Episodes(情景回想) ─────────────────────────────────────────

def format_episode_skeleton(episode: Episode, overlap: int | None = None) -> dict:
    """第一层:骨架——回想列表直接给,细节靠 episode 详情钻取。"""
    item = {
        "id": episode.id,
        "occurred_at": episode.occurred_at.strftime("%Y-%m-%d"),
        "title": episode.title,
        "intent": episode.intent,
        "outcome": episode.outcome,
        "root_cause": episode.root_cause,
        "lesson": episode.lesson,
    }
    if overlap is not None:
        item["overlap"] = overlap
    if episode.enzyme_ids:
        item["enzyme_ids"] = episode.enzyme_ids[:5]
    return item


def format_episode_detail(episode: Episode) -> dict:
    """第二层:整幕——事件链、实体、报错签名、原文摘录。"""
    detail = format_episode_skeleton(episode)
    detail.update({
        "events": episode.events,
        "entities": episode.entities,
        "error_signatures": episode.error_signatures,
        "excerpt": episode.excerpt,
        "domain": episode.domain,
        "session_id": episode.session_id,
        "salience": round(episode.salience, 2),
    })
    return detail


def format_enzyme_detail(enzyme: Enzyme, related: list[dict] | None = None) -> dict:
    """Format single enzyme for detailed view."""
    detail = {
        "id": enzyme.id,
        "type": enzyme.type.value,
        "version": enzyme.version,
        "patterns": enzyme.patterns_readable,
        "suggestion": enzyme.then,
        "reason": enzyme.because,
        "severity": enzyme.severity.value,
        "references": enzyme.references,
        "confidence": round(enzyme.confidence, 3),
        "precision": round(enzyme.precision, 3),
        "stats": {
            "triggers": enzyme.trigger_count,
            "successes": enzyme.success_count,
            "false_positives": enzyme.false_positive_count,
        },
        "origin": enzyme.origin.value,
        "parent_ids": enzyme.parent_ids,
        "domain": enzyme.domain,
        "tags": enzyme.tags,
        # 经验(System 1 式识别):被证实适用 / 不适用过的真实情境
        "exemplars": enzyme.exemplars,
        "anti_exemplars": enzyme.anti_exemplars,
    }
    if related:
        detail["related"] = [
            {"id": r.get("id", ""), "relation": r.get("relation", "")}
            for r in related[:5]
        ]
    return detail


def format_instructions(context_enzymes: list[Enzyme], universal_enzymes: list[Enzyme]) -> str:
    """Format enzymes for dynamic MCP instructions."""
    parts = ["【认知酶·安全直觉】\n"]

    if context_enzymes:
        parts.append("🎯 当前上下文:")
        for e in context_enzymes:
            line = f"- {e.then}（置信度 {e.confidence:.0%}）"
            if e.references:
                line += f"\n  参考: {', '.join(e.references[:2])}"
            parts.append(line)

    if universal_enzymes:
        parts.append("\n🧠 通用直觉:")
        for e in universal_enzymes:
            parts.append(f"- 看到 {e.patterns_readable} → {e.then}")

    parts.append("\n使用直觉后请 report()，发现新经验请 harvest()。")
    return "\n".join(parts)
