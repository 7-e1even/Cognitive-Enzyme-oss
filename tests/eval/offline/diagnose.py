"""Join reviewed memory labels to saved decisions. Pure offline computation."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from tests.eval.offline.records import DATA, digest, read_rows, verify

ANNOTATIONS = Path(__file__).parent / "annotations/math_physics.v2.json"
DEFAULT_OUTPUT = DATA / "math-physics-labels-2026-10-16"
DEFAULT_SOURCES = DATA / "math-physics-labels-2026-10-15/sources"
RELATIONS = {"direct", "conditional", "method_hint", "not_applicable", "mixed_content"}
INVALID_QUESTION_KINDS = {"inconsistent_quantifier", "empty_question", "missing_setup",
                          "missing_dependency", "missing_inputs", "missing_boundary_conditions",
                          "undefined_variable", "broken_sum_index", "undefined_colour"}
USABLE_STAGES = {"hit", "post_selection_injection_mismatch", "selection_rejected", "selection_input_truncated",
                 "selection_input_unverified", "upstream_unknown", "selector_service_or_trace_missing",
                 "judged_pool_rejected_recall_unknown"}


def normalize(text: str) -> str:
    return " ".join(text.split())


def context_hash(text: str) -> str:
    # The public benchmark texts contain no secret patterns. Exact hash joins
    # fail closed rather than guessing from the judge's truncated state.
    return hashlib.sha1(normalize(text).encode()).hexdigest()[:20]


def rule_body(decision: dict, candidate: dict) -> str:
    question = (decision.get("request") or {}).get("questions", {}).get(candidate["key"], {})
    rule = (question.get("instructions") or {}).get("rule", {})
    return rule.get("建议", "") if isinstance(rule, dict) else ""


def rule_digest(decision: dict, candidate: dict) -> str:
    rule = (decision.get("request") or {}).get("questions", {}).get(candidate["key"], {}).get("instructions", {}).get("rule", {})
    return digest(json.dumps(rule, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def body_matches(label: dict, delivered: str, memory: dict) -> bool:
    # A keyword in a truncated prefix is not proof that the useful method was
    # delivered. Every shortened/different body needs its own reviewed witness.
    return bool(delivered) and normalize(memory["then"]) == normalize(delivered) and all(
        anchor in delivered for anchor in label["historical_body_requires"])


def score_facts(decision: dict, candidate: dict) -> dict:
    """Recorded score comparisons, not a replay of all production channels.

    The relative supplement gate is q >= 0.1 * max(q), pipeline.py:352
    (git blame 23701e3e, 2026-09-25, before these experiment records).
    """
    q_max = max((c.get("q") or 0 for c in decision["candidates"]), default=0)
    t = decision.get("policy", {}).get("rule", {})
    p, q = candidate.get("p"), candidate.get("q")
    return {"max_choice_probability": q_max, "relative_supplement_q_floor": .1 * q_max,
            "passes_relative_supplement_q": q >= .1 * q_max if q is not None else None,
            "supplement_p_threshold": t.get("supplement_p"),
            "passes_supplement_p": p >= t["supplement_p"] if p is not None and "supplement_p" in t else None}


def select_decisions(problem: dict, row: dict, decisions: list[dict]) -> tuple[list[dict], str]:
    matching = [d for d in decisions if d["context_hash"] == context_hash(problem["problem"])]
    exact = [d for d in matching if [x["id"] for x in d["injected"]] == row.get("injected", [])]
    if exact:
        return exact, "exact_context_and_injection" if len(exact) == 1 else "multiple_matching_decisions"
    return (matching, "unique_context_injection_difference") if len(matching) == 1 else ([], "unresolved_decision_join")


def attribute_pair(problem: dict, row: dict, label: dict, memory: dict, decision: dict,
                   witnesses: list[dict] = (), changes: list[str] = ()) -> dict:
    mid = label["memory_id"]
    candidate = next((c for c in decision["candidates"] if c["id"] == mid), None)
    state = (decision.get("request") or {}).get("state", "")
    full, shown = normalize(problem["problem"]), normalize(state)
    out = {"decision_id": decision["id"], "decision_time": decision["created_at"],
           "state_chars": len(state), "question_chars": len(problem["problem"]),
           "state_truncated": bool(shown) and full.startswith(shown) and len(shown) < len(full),
           "memory_created_at": memory["created_at"], "snapshot_version": memory["version"]}
    if datetime.fromisoformat(memory["created_at"]) > datetime.fromisoformat(decision["created_at"]):
        return {**out, "stage": "not_yet_learned"}
    if candidate is None:
        # Absence from the judged pool is not absence from the recall pool.
        # Feedback/exemplar writes also update updated_at. Only recorded content
        # edits make the final body unsafe to project back into this experiment.
        content_changed_later = any(datetime.fromisoformat(t) > datetime.fromisoformat(decision["created_at"])
                                    for t in changes)
        stage = "historical_version_unknown" if content_changed_later else "upstream_unknown"
        return {**out, "stage": stage}
    body = rule_body(decision, candidate)
    out.update(logged_version=candidate.get("version"), recall_rank=candidate.get("recall_rank"),
               rerank=candidate.get("rerank"), rescued=candidate.get("rescued"),
               p=candidate.get("p"), q=candidate.get("q"), delivered_rule=body,
               none_probability=(((decision.get("answers") or {}).get("best") or {}).get("probabilities") or {}).get("none"),
               policy=decision["policy"], final_injected=mid in row.get("injected", []),
               decision_injected=mid in {r["id"] for r in decision["injected"]},
               selection_score_facts=score_facts(decision, candidate))
    review = next((w for w in witnesses if w["decision_id"] == decision["id"]
                   and w["question_id"] == problem["id"] and w["memory_id"] == mid
                   and w["delivered_body_sha256"] == digest(body.encode())
                   and (not w.get("delivered_rule_sha256") or w["delivered_rule_sha256"] == rule_digest(decision, candidate))), None)
    if review:
        out["historical_review"] = review["rationale"]
        out["reviewed_relation"] = review["relation"]
        if review["relation"] != "direct":
            return {**out, "stage": "historical_" + review["relation"]}
    elif not body_matches(label, body, memory):
        return {**out, "stage": "historical_version_unreviewed"}
    else:
        out["reviewed_relation"] = label["relation"]
        if label["relation"] != "direct":
            return {**out, "stage": "historical_" + label["relation"]}
    if mid in row.get("injected", []):
        return {**out, "stage": "hit"}
    if mid in {r["id"] for r in decision["injected"]}:
        return {**out, "stage": "post_selection_injection_mismatch"}
    if decision["policy"].get("jev_status") != "ok" or candidate.get("p") is None:
        return {**out, "stage": "selector_service_or_trace_missing"}
    if candidate.get("recall_rank") is None:
        return {**out, "stage": "judged_pool_rejected_recall_unknown"}
    if shown != full:
        return {**out, "stage": "selection_input_truncated" if out["state_truncated"] else "selection_input_unverified"}
    return {**out, "stage": "selection_rejected"}


def verify_sources(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    for key, entry in manifest["files"].items():
        path = directory / key
        if not path.resolve().is_relative_to(directory.resolve()) or digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError(f"导出记录已变动：{key}")
    return manifest


def validate_annotations(spec: dict, problems: dict, banks: dict, decisions: dict) -> dict:
    """Validate every label and old-version witness, including unused witnesses."""
    rules, pair_catalogs, rule_ids = defaultdict(list), {}, set()
    for label in spec["rules"]:
        if label["id"] in rule_ids or label["relation"] not in RELATIONS:
            raise ValueError(f"重复或无效标注：{label['id']}")
        rule_ids.add(label["id"])
        memory = banks[label["catalog"]][label["memory_id"]]
        if memory["version"] != label["memory_version"] or digest(memory["then"].encode()) != label["memory_sha256"]:
            raise ValueError(f"标注记忆正文或版本变化：{label['id']}")
        if not label["historical_body_requires"] or not all(a and a in memory["then"] for a in label["historical_body_requires"]):
            raise ValueError(f"适用部分未出现在标注记忆正文：{label['id']}")
        for q in label["questions"]:
            key = (q["id"], label["memory_id"])
            if key in pair_catalogs:
                raise ValueError(f"重复题目—记忆配对：{key}")
            pair_catalogs[key] = label["catalog"]
            if digest(problems[q["id"]]["problem"].encode()) != q["problem_sha256"]:
                raise ValueError(f"标注题面变化：{q['id']}")
            rules[q["id"]].append(label)
    witnessed = set()
    for w in spec.get("historical_witnesses", []):
        key = (w["question_id"], w["memory_id"], w["decision_id"])
        if key in witnessed or w["relation"] not in RELATIONS:
            raise ValueError(f"重复或无效历史复核：{key}")
        witnessed.add(key)
        db = pair_catalogs.get(key[:2])
        d = decisions.get(db, {}).get(w["decision_id"])
        candidate = next((c for c in d["candidates"] if c["id"] == w["memory_id"]), None) if d else None
        if (not d or d["context_hash"] != context_hash(problems[w["question_id"]]["problem"])
                or not candidate or candidate.get("version") != w["logged_version"]
                or digest(rule_body(d, candidate).encode()) != w["delivered_body_sha256"]
                or (w.get("delivered_rule_sha256") and w["delivered_rule_sha256"] != rule_digest(d, candidate))):
            raise ValueError(f"历史版本证据变化或未找到：{key}")
    for w in spec.get("content_warnings", []):
        memory = banks[w["catalog"]][w["memory_id"]]
        if (memory["version"] != w["memory_version"] or digest(memory["then"].encode()) != w["memory_sha256"]
                or not all(quote in memory["then"] for quote in w["claim_excerpts"])):
            raise ValueError(f"内容反例的原文或版本变化：{w['alias']}")
    triaged = spec.get("triage", {}).get("question_ids", [])
    if len(triaged) != len(set(triaged)) or any(pid not in problems for pid in triaged):
        raise ValueError("初筛题目清单存在重复或未知题目")
    return rules


def problematic_injections(ledger: list[dict], pairs: list[dict]) -> list[dict]:
    observations = [{**p, "question_id": p["id"]} for p in pairs]
    observations.extend({**a, "question_id": q["id"]} for q in ledger for a in q["other_annotation_evidence"])
    issues = []
    for item in observations:
        evidence = item["evidence"]
        if not evidence or not all(e.get("final_injected") and e.get("reviewed_relation") in
                                   {"mixed_content", "not_applicable"} for e in evidence):
            continue
        first = evidence[0]
        issues.append({"question_id": item["question_id"], "memory_id": item["memory_id"],
                       "annotation_id": item["annotation_id"], "logged_version": first["logged_version"],
                       "relations": sorted({e["reviewed_relation"] for e in evidence}),
                       "rationale": first.get("historical_review", item["rationale"]),
                       "p": first["p"], "q": first["q"],
                       "decision_ids": [e["decision_id"] for e in evidence],
                       "body_sha256": [digest(e["delivered_rule"].encode()) for e in evidence]})
    return issues


def diagnose(record: Path, sources: Path, annotations: Path) -> tuple[dict, list[dict], list[dict]]:
    verify(record)
    exported = verify_sources(sources)
    spec = json.loads(annotations.read_text())
    problems = {p["id"]: p for p in json.loads((record / "hardbench/problems.json").read_text())}
    banks, decision_banks = {}, {}
    for db, identity in spec["banks"].items():
        path = sources / db / "enzymes.jsonl"
        if digest(path.read_bytes()) != identity["sha256"]:
            raise ValueError(f"标注使用了不同的记忆库：{db}")
        banks[db] = {m["id"]: m for m in read_rows(path)}
        decision_banks[db] = {d["id"]: d for d in read_rows(sources / db / "decision_log.jsonl")}
    rules = validate_annotations(spec, problems, banks, decision_banks)
    triaged = set(spec.get("triage", {}).get("question_ids", []))
    ledger, pairs = [], []
    for tag, group, arm in (("math", "math", "self"), ("phys", "physics", "self_v3")):
        db = f"ce_rsi_{tag}_{arm}"
        meta = json.loads((record / f"hardbench/{group}/history/{tag}.meta.json").read_text())
        history = {r["id"]: r for r in read_rows(record / f"hardbench/{group}/history/{tag}.{arm}.jsonl")}
        decisions = list(decision_banks[db].values())
        changes = defaultdict(list)
        for event in read_rows(sources / db / "evolution_log.jsonl"):
            if event["event_type"] == "consolidation":
                changes[event["enzyme_id"]].append(event["created_at"])
        for pid in meta["order"]:
            p, row = problems[pid], history.get(pid)
            issue = spec.get("question_issues", {}).get(pid)
            reviewed_history_ids = {w["memory_id"] for w in spec.get("historical_witnesses", [])
                                   if w["question_id"] == pid and w["relation"] == "direct"}
            labels = [r for r in rules[pid] if r["catalog"] == db and
                      (r["relation"] == "direct" or r["memory_id"] in reviewed_history_ids)]
            candidates, join = select_decisions(p, row, decisions) if row else ([], "not_run")
            ledger.append({"id": pid, "group": group, "arm": arm, "has_saved_run": row is not None,
                           "initial_review_done": pid in triaged,
                           "problem_sha256": digest(p["problem"].encode()),
                           "final_library_positive_ids": sorted({l["memory_id"] for l in labels if l["relation"] == "direct"}),
                           "expected_memory_ids": [],
                           "conditional_memory_ids": [r["memory_id"] for r in rules[pid] if r["relation"] == "conditional"],
                           "method_hint_ids": [r["memory_id"] for r in rules[pid] if r["relation"] == "method_hint"],
                           "mixed_content_ids": [r["memory_id"] for r in rules[pid] if r["relation"] == "mixed_content"],
                           "question_issue": issue,
                           "negative_memory_ids": [r["memory_id"] for r in rules[pid] if r["relation"] == "not_applicable"],
                           "join": join, "matching_decisions": len(candidates),
                           "exhaustive_labels": False, "other_annotation_evidence": []})
            # Keep non-positive exposures too. An old clean version may be a
            # positive even if the final library contains mixed or wrong claims.
            for label in [r for r in rules[pid] if r["catalog"] == db and r not in labels]:
                memory = banks[db][label["memory_id"]]
                ledger[-1]["other_annotation_evidence"].append({
                    "annotation_id": label["id"], "memory_id": memory["id"],
                    "relation": label["relation"], "rationale": label["rationale"],
                    "evidence": [attribute_pair(p, row, label, memory, d, spec.get("historical_witnesses", []),
                                                changes[memory["id"]]) for d in candidates]})
            for label in labels:
                memory = banks[db][label["memory_id"]]
                observed = [attribute_pair(p, row, label, memory, d, spec.get("historical_witnesses", []),
                                           changes[memory["id"]]) for d in candidates]
                stages = {o["stage"] for o in observed}
                stage = next(iter(stages)) if len(stages) == 1 else "ambiguous_decision" if stages else join
                if issue and issue["kind"] in INVALID_QUESTION_KINDS:
                    stage = "question_invalid_or_incomplete"
                pairs.append({"id": pid, "group": group, "memory_id": label["memory_id"],
                              "annotation_id": label["id"], "annotation_relation": label["relation"],
                              "rationale": label["rationale"], "stage": stage,
                              "evidence": observed})
                if stage in USABLE_STAGES:
                    ledger[-1]["expected_memory_ids"].append(memory["id"])
            ledger[-1]["label_status"] = ("reviewed_historical_positives_partial" if ledger[-1]["expected_memory_ids"]
                                           else "final_library_only" if ledger[-1]["final_library_positive_ids"]
                                           else "historical_target_not_attributable" if labels
                                           else "reviewed_no_clean_target_confirmed" if pid in triaged else "not_reviewed")
    summary = {"schema": "offline-memory-diagnosis-v1", "api_calls": 0,
               "annotation_sha256": digest(annotations.read_bytes()), "exported_files": len(exported["files"]),
               "source_manifest_sha256": digest((sources / "manifest.json").read_bytes()),
               "record_manifest_sha256": digest((record / "manifest.json").read_bytes()),
               "target_arms": {"math": "self", "physics": "self_v3"},
               "annotation_scope": spec["completeness"], "reviewer": spec["reviewer"],
               "reviewed_rules": len(spec["rules"]),
               "reviewed_pairs": sum(len(r["questions"]) for r in spec["rules"]),
               "historical_version_reviews": len(spec.get("historical_witnesses", [])),
               "content_warnings": spec.get("content_warnings", []),
               "historical_content_warnings": spec.get("historical_content_warnings", []),
               "problematic_injections": problematic_injections(ledger, pairs),
               "question_issues": spec.get("question_issues", {}), "groups": {}}
    for group in ("math", "physics"):
        cases = [r for r in ledger if r["group"] == group]
        positive = [r for r in pairs if r["group"] == group]
        misses = {r["id"] for r in positive if r["stage"] == "selection_rejected"}
        hits = {r["id"] for r in positive if r["stage"] == "hit"}
        db = "ce_rsi_math_self" if group == "math" else "ce_rsi_phys_self_v3"
        nonempty_hashes = {d["context_hash"] for d in decision_banks[db].values() if d["candidates"]}
        summary["groups"][group] = {"planned_questions": len(cases),
            "saved_questions": sum(r["has_saved_run"] for r in cases),
            "initially_reviewed_saved_questions": sum(r["initial_review_done"] and r["has_saved_run"] for r in cases),
            "unrun_questions": sum(not r["has_saved_run"] for r in cases),
            "labelled_questions": sum(bool(r["final_library_positive_ids"]) for r in cases),
            "historically_usable_labelled_questions": sum(bool(r["expected_memory_ids"]) for r in cases),
            "labelled_saved_questions": sum(bool(r["expected_memory_ids"]) and r["has_saved_run"] for r in cases),
            "saved_without_clean_final_target": sum(r["has_saved_run"] and not r["final_library_positive_ids"] for r in cases),
            "saved_without_confirmed_historical_target": sum(r["has_saved_run"] and not r["expected_memory_ids"] for r in cases),
            "question_issues": sum(r["question_issue"] is not None for r in cases),
            "positive_pairs": len(positive), "stages": dict(Counter(p["stage"] for p in positive)),
            "selection_rejection_questions": len(misses),
            "selection_rejection_without_any_labelled_hit": len(misses - hits),
            "reviewed_problematic_injections": sum(i["question_id"] in {c["id"] for c in cases}
                                                   for i in summary["problematic_injections"]),
            "saved_with_judged_pool": sum(context_hash(problems[r["id"]]["problem"]) in nonempty_hashes
                                          for r in cases if r["has_saved_run"]),
            "recall_failures_identifiable": False, "whole_dataset_recall": None}
    return summary, ledger, pairs


def render(summary: dict, pairs: list[dict]) -> str:
    misses = [p for p in pairs if p["stage"] == "selection_rejected"]
    judge_models = sorted({e["policy"]["model"] for p in misses for e in p["evidence"] if e["policy"].get("model")})
    lines = ["# 数学、物理记忆标注与阶段诊断", "", "本次只使用已保存文件和本地实验库的只读导出，API 调用 0 次。", "",
             f"已确认 {len({p['id'] for p in misses})} 道题存在选择阶段的漏选，涉及 {len(misses)} 个题目—记忆对。"
             "这证明选择环节存在问题，但现有日志不能比较召回与选择的总体失败比例。", "",
             "## 标注覆盖", "",
             "正标签由本轮 agent 核对题面、解法和记忆适用条件给出，未经过独立人工复核。"
             "这是已确认正例的部分集合，未标记不等于无正确记忆；不能据此计算全题集召回率。", "",
             "分析对象是数学 `self` 与物理 `self_v3` 的原经验流；不混入合成查询、teacher/seed 对照或后来的冻结库重测。"
             "标注同时固定题面、记忆 ID、正文 SHA-256 和版本；历史版本另附实际送给判断器的正文哈希。", "",
             f"共 {summary['reviewed_pairs']} 个已审阅配对，分为具体正例、方法提示、条件适用、不适用与混合错误内容；"
             f"另复核 {summary['historical_version_reviews']} 份实际交付的历史正文。初筛覆盖全部已保存运行题，正例集合仍非穷尽。", "",
             "| 集合/arm | 计划 | 有记录且已初筛 | 未运行 | 最终库有具体正例的题 | 历史时刻确认可用的题 | 已运行但未确认最终库正例 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for group, s in summary["groups"].items():
        lines.append(f"| {group}/{summary['target_arms'][group]} | {s['planned_questions']} | "
                     f"{s['initially_reviewed_saved_questions']} | {s['unrun_questions']} | {s['labelled_questions']} | "
                     f"{s['historically_usable_labelled_questions']} | {s['saved_without_clean_final_target']} |")
    lines += ["", "“历史可用”要求记忆已创建且已确认适用的内容当时存在；"
              "包括未进入判断池但没有后续正文修改记录的条目。正文后来改过、又没有旧版本的条目不算已确认。"
              "未确认正例的题保留弃权状态，不标成无相关记忆的负例。", ""]
    lines += ["", "## 能确认到哪个环节", "",
              "按题目—已标记记忆对计数，同题多个替代记忆分别保留；各桶不是全题集问题占比。", "",
              "| 集合 | 有用记忆已注入 | 召回且入判断池后漏选 | 判断输入截断 | 做题时尚未学到 | 历史版本待复核/未知 | 上游未知 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for group, stats in summary["groups"].items():
        s = stats["stages"]
        lines.append(f"| {group} | {s.get('hit',0)} | {s.get('selection_rejected',0)} | {s.get('selection_input_truncated',0)} | "
                     f"{s.get('not_yet_learned',0)} | {s.get('historical_version_unknown',0)+s.get('historical_version_unreviewed',0)} | "
                     f"{s.get('upstream_unknown',0)} |")
    lines += ["", "进入判断池且留有 recall_rank 的正例能够排除本次首段漏召回；"
              "候选池之外未保存完整 recall/rerank 集合，所以缺席不能区分未召回、精排淘汰或池容量截断。", "",
              "只将当时交给判官的正文与已复核正文一致、且包含适用部分的记录纳入明确的命中/漏选。"
              "创建时间晚于做题时间的记忆单列，避免用最终库倒推历史。"
              "最终库正例中有不少来自该题作答后的提炼；这一桶不代表召回失败，也不能当作内容不足的总体比例。", "",
              "## 可核对的选择漏选案例", ""]
    if judge_models:
        lines += ["这些选择分数来自历史判断模型 " + ", ".join(f"`{m}`" for m in judge_models) +
                  "；不把后续解题模型的答案当作选择判断。", ""]
    for p in misses[:25]:
        e = p["evidence"][0]
        rationale = e.get("historical_review", p["rationale"]).rstrip("。")
        lines += [f"- **{p['id']}** → `{p['memory_id']}`：{rationale}。"
                  f"召回名次 {e['recall_rank']}，p={e['p']}，q={e['q']}，none={e['none_probability']}；"
                  f"当时版本 {e['logged_version']}，未进入最终注入。判定 ID：`{e['decision_id']}`。"]
    lines += ["", "p 是原判断器的适用性输出，q 是相对选择概率，none 是放弃选择的概率；它们只作历史证据，不充当正确标签。"
              "同题已注入其他相关记忆时，漏掉某个替代记忆也不必然导致解题失败；这些结果不能直接解释正确率差异。", ""]
    with_other_hit = sum(s["selection_rejection_questions"] - s["selection_rejection_without_any_labelled_hit"]
                         for s in summary["groups"].values())
    lines += [f"这 {len({p['id'] for p in misses})} 道题中，{with_other_hit} 道还注入了其他已核实的有用记忆；"
              "复合题中可能只覆盖了别的子问。未标到正例也不能当成整题没有获得帮助。", "",
              "下面仅核对历史分数与补充门的数值关系，不模拟危急级、类比等全部通道：", ""]
    for p in misses:
        e = p["evidence"][0]
        facts = e["selection_score_facts"]
        if facts["passes_supplement_p"] and not facts["passes_relative_supplement_q"]:
            lines.append(f"- `{p['id']}`：p={e['p']:.4f} 达到补充门 {facts['supplement_p_threshold']}，"
                         f"但 q={e['q']:.4f} < 0.1×q_max={facts['relative_supplement_q_floor']:.4f}，"
                         "不满足补充通道的相对概率条件。")
    lines += ["", "## 判断输入与最终注入记录", "",
              "以下单列，不归为判断器看过完整输入后的漏选：", "",
              "| 题目 | 状态 | 判断题面字符数 | 完整题面字符数 | 标注 |", "|---|---|---:|---:|---|"]
    for p in pairs:
        if p["stage"] in {"selection_input_truncated", "post_selection_injection_mismatch"}:
            e = p["evidence"][0]
            lines.append(f"| {p['id']} | {p['stage']} | {e['state_chars']} | {e['question_chars']} | {p['annotation_id']} |")
    lines += ["", "`post_selection_injection_mismatch` 表示同一题面仅找到一份判断记录，其中已选条目在最终运行记录中缺失。"
              "可能发生于后续过滤或记录不同步，现有记录不能确认具体原因。", ""]
    lines += ["", "## 记忆内容问题", ""]
    for warning in summary["content_warnings"]:
        lines.append(f"- `{warning['alias']}`：{warning['proof']}")
    for warning in summary["historical_content_warnings"]:
        lines.append(f"- 历史 `{warning['alias']}` v{warning['logged_version']}（{warning['question_id']}）：{warning['proof']}")
    lines += ["", "上述问题只在标注中隔离，本轮没有改写或删除记忆。正确片段与错误推广混在同一条里，不能靠提高召回率解决。", "",
              "## 已注入内容的反例", "",
              "下表均按实际交付的历史正文复核。混合内容表示正确片段里夹有错误断言，不等同于整条毫无帮助；"
              "不适用表示当时的具体模板不符合题设。", "",
              "| 题目 | 标注 | 历史版本 | 类型 | 原判断 p |", "|---|---|---:|---|---:|"]
    for issue in summary["problematic_injections"]:
        lines.append(f"| {issue['question_id']} | {issue['annotation_id']} | {issue['logged_version']} | "
                     f"{', '.join(issue['relations'])} | {issue['p']:.4f} |")
    lines += ["", "具体理由、判定 ID 和正文哈希保存在 `problematic_injections.jsonl`；"
              "不能由这些注入记录推断最终答案受到了多大影响。", "",
              "## 题面与参考记录问题", ""]
    for pid, issue in summary["question_issues"].items():
        lines.append(f"- `{pid}`：`{issue['kind']}`；{issue.get('note', issue.get('reason', '见标注文件'))}")
    lines += ["", "## 完整阶段计数", "", "以下保留不适用历史版本、服务失败、未运行等全部桶，避免遗漏未能归因的记录。", ""]
    for group, stats in summary["groups"].items():
        lines.append(f"- {group}：`{json.dumps(stats['stages'], ensure_ascii=False)}`")
    lines += ["", "## 结论边界与下一步", "",
              "- 正标签继续按题目适用条件复核，通用提醒与具体知识分开；不能把原判官输出或原注入直接当标准。",
              "- 先以明确漏选和已注入反例作固定离线集，核对原 p/q、放弃选择和补充门的取舍；不凭少数正例统一降低阈值。",
              "- 先修正文中的错误断言和适用条件，再讨论检索优化；本轮没有修改线上选择参数或架构。",
              "- 判断池没有保存完整首段候选；现有记录无法补造这部分。下一次获准采集时再保存全阶段 trace。",
              "- 数学还有计划题未运行，未运行不算召回失败；同源或近重复题不能当独立泛化证据。",
              "- 本轮结果不验证记忆带来的解题增益，也不重跑已保存的模型答案。", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, default=DATA / "2026-10-15")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--annotations", type=Path, default=ANNOTATIONS)
    args = parser.parse_args()
    summary, ledger, pairs = diagnose(args.record, args.sources, args.annotations)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "question_labels.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in ledger))
    (args.output / "pair_diagnoses.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in pairs))
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    (args.output / "problematic_injections.jsonl").write_text("".join(
        json.dumps(r, ensure_ascii=False)+"\n" for r in summary["problematic_injections"]))
    (args.output / "report.md").write_text(render(summary, pairs))
    # Preserve the exact labels and analysis code next to the derived records.
    # This is separate from both immutable input snapshots.
    method = args.output / "method"
    method.mkdir(exist_ok=True)
    for name, path in (("annotations.json", args.annotations), ("diagnose.py", Path(__file__)),
                       ("records.py", Path(__file__).with_name("records.py"))):
        (method / name).write_bytes(path.read_bytes())
    outputs = ["question_labels.jsonl", "pair_diagnoses.jsonl", "problematic_injections.jsonl",
               "summary.json", "report.md", "method/annotations.json", "method/diagnose.py", "method/records.py"]
    bundle = {"schema": "offline-memory-analysis-bundle-v1", "api_calls": 0,
              "record_manifest_sha256": summary["record_manifest_sha256"],
              "source_manifest_sha256": summary["source_manifest_sha256"],
              "files": {name: {"sha256": digest((args.output / name).read_bytes())} for name in outputs}}
    (args.output / "analysis_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k: summary[k] for k in ("api_calls", "reviewed_rules", "reviewed_pairs",
                                             "historical_version_reviews", "groups")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
