"""Mutation — create variants of mature enzymes to expand coverage.

LLM-enhanced: uses LLM to generate better rephrase alternatives.
Falls back to synonym table if LLM is not configured.

Constraints:
- Only mutate enzymes with confidence > 0.7 and trigger_count > 5
- Mutants start with confidence = 0.3 (probation)
- Mutants with precision < 0.3 after 5 triggers are auto-pruned
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone

from app.core import (
    Enzyme,
    EnzymeRelation,
    EvolutionEvent,
    EventType,
    Origin,
    Pattern,
    PatternType,
    RelationType,
)
from app.storage.pg_store import PGStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)

STRATEGIES = ["broaden", "specialize", "rephrase"]


def _mutant_signature(e: Enzyme) -> tuple:
    """内容级指纹:broaden/specialize 是确定性变换,每轮巩固对同一亲本产出的
    mutant 逐字节相同——按内容去重,不再每轮堆一条同内容新 mutant。"""
    return (
        e.parent_ids[0] if e.parent_ids else "",
        e.then,
        tuple(sorted(p.value.lower() for p in e.patterns)),
        tuple(sorted(str(c).lower() for c in e.conditions)),
    )


async def mutate_enzyme(
    parent: Enzyme,
    strategy: str,
    pg: PGStore,
    sync: SyncManager,
    existing_signatures: set[tuple] | None = None,
) -> Enzyme | None:
    """Create a mutant variant of a mature enzyme."""
    if parent.status != "active" or parent.confidence < 0.7 or parent.trigger_count < 5:
        return None

    mutant = Enzyme(
        type=parent.type,
        patterns=copy.deepcopy(parent.patterns),
        conditions=list(parent.conditions),
        negations=list(parent.negations),
        then=parent.then,
        because=f"[mutant:{strategy}] {parent.because}",
        severity=parent.severity,
        references=list(parent.references),
        confidence=0.45,
        precision=0.5,
        origin=Origin.MUTATED,
        parent_ids=[parent.id],
        domain=parent.domain,
        tags=list(parent.tags),
    )

    if strategy == "broaden" and mutant.conditions:
        removed = mutant.conditions.pop(0)
        mutant.because = f"[broaden: 移除条件 '{removed}'] {parent.because}"

    elif strategy == "specialize" and parent.false_positive_count > 0:
        if parent.domain:
            mutant.conditions.append(f"domain:{parent.domain}")
            mutant.because = f"[specialize: 限定 {parent.domain}] {parent.because}"
        else:
            return None

    elif strategy == "rephrase":
        # Try LLM first, fallback to synonym table
        new_patterns = await _generate_alternatives_llm(parent)
        if not new_patterns:
            new_patterns = _generate_alternatives_rule(parent.patterns)
        if new_patterns:
            mutant.patterns.extend(new_patterns)
            mutant.because = f"[rephrase: +{len(new_patterns)} 模式] {parent.because}"
        else:
            return None
    else:
        return None

    # 内容级去重:库里已有同内容 mutant(含被隔离待审的)就不再重复创建
    if existing_signatures is not None and _mutant_signature(mutant) in existing_signatures:
        return None

    # Save
    await sync.sync_enzyme(mutant, await pg.upsert_enzyme(mutant))

    await pg.upsert_relation(EnzymeRelation(
        source_id=mutant.id,
        target_id=parent.id,
        relation=RelationType.EVOLVED_FROM,
    ))

    await pg.log_event(EvolutionEvent(
        enzyme_id=mutant.id,
        event_type=EventType.MUTATION,
        detail={"parent_id": parent.id, "strategy": strategy, "parent_confidence": parent.confidence},
    ))

    logger.info("Mutation [%s]: %s → %s", strategy, parent.id, mutant.id)
    return mutant


async def prune_failed_mutants(pg: PGStore, sync: SyncManager) -> int:
    """Remove mutants that failed probation (trigger>=5, precision<0.3)."""
    enzymes = await pg.list_enzymes(limit=10000)
    pruned = 0
    for enzyme in enzymes:
        if (enzyme.origin == Origin.MUTATED
                and enzyme.trigger_count >= 5
                and enzyme.precision < 0.3):
            await pg.log_event(EvolutionEvent(
                enzyme_id=enzyme.id,
                event_type=EventType.MUTATION,
                detail={"action": "prune_failed_mutant", "precision": enzyme.precision},
            ))
            await pg.delete_enzyme(enzyme.id)
            await sync.sync_delete(enzyme.id)
            pruned += 1
    if pruned:
        logger.info("Pruned %d failed mutants", pruned)
    return pruned


async def run_mutation_cycle(pg: PGStore, sync: SyncManager, max_mutants: int = 5) -> dict:
    """Run one mutation cycle."""
    candidates = await pg.list_enzymes(min_confidence=0.7, limit=20)
    candidates = [e for e in candidates if e.trigger_count >= 5]
    # 已有 mutant 的内容指纹(轮次内共享,创建成功后即时补充)
    existing = await pg.list_enzymes(limit=10000)
    signatures = {
        _mutant_signature(e) for e in existing
        if e.origin == Origin.MUTATED and e.status != "rejected"
    }
    created = 0
    for enzyme in candidates:
        if created >= max_mutants:
            break
        for strategy in STRATEGIES:
            mutant = await mutate_enzyme(enzyme, strategy, pg, sync, existing_signatures=signatures)
            if mutant:
                signatures.add(_mutant_signature(mutant))
                created += 1
                break
    pruned = await prune_failed_mutants(pg, sync)
    return {"created": created, "pruned": pruned}


# ── LLM-enhanced alternative generation ──────────────────────

async def _generate_alternatives_llm(parent: Enzyme) -> list[Pattern]:
    """Use LLM to generate better rephrase alternatives."""
    from app.llm_client import chat_json

    patterns_text = ", ".join(p.value for p in parent.patterns)
    prompt = f"""你是安全研究专家。给定一个安全检测规则的匹配关键词，生成额外的变体关键词来扩大覆盖范围。

当前关键词: {patterns_text}
规则建议: {parent.then[:200]}
领域: {parent.domain or '通用安全'}

请生成 3-5 个补充关键词（可以包括：同义词、缩写、英文/中文翻译、相关类名/方法名、CVE 编号等）。
这些关键词应该能帮助匹配到与当前规则相关但未覆盖的场景。

返回 JSON 数组格式: [{{"value": "关键词", "reason": "为什么加这个"}}]"""

    result = await chat_json(
        system="你是安全领域专家，帮助扩展安全检测规则的覆盖范围。只返回 JSON。",
        user=prompt,
    )

    if not result or not isinstance(result, list):
        return []

    existing = {p.value.lower() for p in parent.patterns}
    alternatives = []
    for item in result:
        val = item.get("value", "").strip()
        if val and val.lower() not in existing:
            alternatives.append(Pattern(type=PatternType.KEYWORD, value=val, weight=0.8))
            existing.add(val.lower())

    logger.info("LLM generated %d alternatives for enzyme %s", len(alternatives), parent.id)
    return alternatives[:5]


# ── Rule-based fallback ───────────────────────────────────────

_ALTERNATIVES: dict[str, list[str]] = {
    "readobject": ["readResolve", "readExternal", "ObjectInput"],
    "deserialization": ["反序列化", "unmarshal", "decode"],
    "反序列化": ["deserialization", "readObject"],
    "injection": ["注入", "inject"],
    "注入": ["injection", "inject"],
    "bypass": ["绕过", "evasion", "circumvent"],
    "绕过": ["bypass", "evasion"],
    "upload": ["文件上传", "MultipartFile"],
    "ssrf": ["server-side request forgery", "url fetch"],
    "xss": ["cross-site scripting", "script injection"],
    "fastjson": ["com.alibaba.fastjson", "parseObject", "JSON.parse"],
    "jndi": ["InitialContext", "lookup", "naming"],
    "shiro": ["rememberMe", "AES-CBC", "CommonsCollections"],
    "thymeleaf": ["SpringTemplateEngine", "th:text"],
    "actuator": ["/env", "/jolokia", "management.endpoints"],
}


def _generate_alternatives_rule(patterns: list[Pattern]) -> list[Pattern]:
    """Generate alternative patterns based on synonym map."""
    alternatives: list[Pattern] = []
    existing = {p.value.lower() for p in patterns}
    for p in patterns:
        key = p.value.lower()
        if key in _ALTERNATIVES:
            for alt in _ALTERNATIVES[key]:
                if alt.lower() not in existing:
                    alternatives.append(Pattern(type=PatternType.KEYWORD, value=alt, weight=p.weight * 0.8))
                    existing.add(alt.lower())
    return alternatives[:3]
