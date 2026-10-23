"""Offline layer attribution and model-conditional memory benefit statistics."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha, stage_for, verify

STAGES = {
    "injected": "正确记忆已注入", "judge_budget_cut": "Jev 请求预算裁剪",
    "rank_or_judge_pool_cut": "排序或判定池上限", "judge_or_selection_rejected": "Jev 分数或选择规则未放行",
    "fused_recall_miss": "融合召回未覆盖", "recall_merge_or_route_drop": "召回合并或分流丢失",
    "ann_miss": "近似向量检索漏项", "index_missing": "索引缺失", "condition_filter": "条件过滤",
    "hook_filter": "注入端过滤", "selector_service_failure": "判定服务失败",
    "collection_error": "采集错误", "recall_service_or_trace_missing": "召回服务或记录缺失",
    "not_collected": "尚未采集",
}


def replay_recall(rec, bank):
    searches = rec.get("dense_searches") or []
    if not searches:
        return None
    dense = searches[0]["hits"]
    scale = [s for _, s in dense]
    best = dict(dense)
    for rank, hit in enumerate(rec.get("lexical_hits") or []):
        score = scale[rank] if rank < len(scale) else scale[-1] if scale else 0
        eid = hit["id"]
        if eid not in best or score > best[eid]:
            best[eid] = score
    best = dict(sorted(best.items(), key=lambda x: -x[1]))
    for hit in rec.get("exemplar_hits") or []:
        if hit["kind"] == "pos":
            best[hit["id"]] = max(best.get(hit["id"], -1), hit["score"])
    ranked = sorted(best, key=best.get, reverse=True)
    merged = ranked[:60]
    route = rec["trace"].get("route") or {}
    merged += route.get("added_ids", [])
    active = {e["id"]: e for e in bank if e.get("status") == "active"}
    merged = [i for i in merged if i in active]
    domains = {d for d, _ in route.get("domains", [])}
    if domains:
        inside = [i for i in merged if active[i]["domain"] in domains]
        merged = inside if route.get("mode") == "strict" else inside + [i for i in merged if active[i]["domain"] not in domains]
    return {"matches": merged == rec["trace"].get("recalled_ids", []), "full_fusion_ranking": ranked}


def score_reason(eid, rec, policy):
    entries = rec["trace"].get("jev") or []
    p = {x[0]: x[1] for x in entries}
    q = {x[0]: x[2] for x in entries}
    if eid not in p:
        return "not_judged"
    top = max(p, key=lambda i: (q[i], p[i]))
    if p[eid] >= policy["selection.supplement_p"] and q[eid] < .1 * max(q.values()):
        return "high_p_relative_q_gate"
    if p[eid] < policy["selection.primary_p"]:
        return "low_absolute_p"
    if rec["trace"].get("jev_none", 0) >= policy["selection.none_max"] and eid != top:
        return "none_blocks_supplement"
    if eid != top and p[eid] < policy["selection.supplement_p"]:
        return "below_supplement_p"
    return "rescue_critical_hint_or_capacity_gate"


def wilson(k, n):
    if not n:
        return None
    z = 1.96
    p = k / n
    center = (p + z*z/(2*n)) / (1 + z*z/n)
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / (1 + z*z/n)
    return [max(0, center-half), min(1, center+half)]


def analyze(directory: Path):
    manifest = verify(directory / "inputs")
    tasks = json.loads((directory / "inputs/tasks.json").read_text())
    banks = json.loads((directory / "inputs/banks.json").read_text())
    recs = {r["id"]: r for r in rows(directory / "retrieval.jsonl")}
    metadata = json.loads((directory / "retrieval-manifest.json").read_text())
    policy = metadata["policy"]
    probes = {(r["id"], r["memory_id"]): r for r in rows(directory / "gold-full-input.jsonl")}
    cases = rows(directory / "assembled-cases.jsonl")
    case_by_id = {r["id"]: r for r in cases}
    details = []
    result = {"input_counts": manifest["counts"], "layers": {}, "need": {}, "input_audit": {},
              "api": {}, "limitations": [
                  "Mature frozen banks, not historical replay; partial agent-reviewed positives.",
                  "Known same/near-source exclusions applied; unrecorded lineage may still leak.",
                  "Same model family grades blind calls; benchmark references can contain undetected defects.",
                  "Closed-book correctness is observed at a fixed reasoning/time budget, not proof of inherent knowledge.",
                  "Single gold/system samples and two closed-book samples do not establish per-question causal certainty.",
                  "No relation snapshot or domain descriptions; extra chat fallback/expansion/exploration disabled.",
                  "Confidence gate sees a complete draft; savings do not include the draft's inference cost."]}
    replay_errors = []
    native_pool_sizes, state_truncated, rule_caps = [], 0, 0
    mapped_candidates, probability_mismatches = 0, []
    for rec in recs.values():
        replay = replay_recall(rec, banks[rec["group"]])
        if replay and not replay["matches"]:
            replay_errors.append(rec["id"])
        native_pool_sizes.append(len(rec["trace"].get("jev_pool") or []))
        state_truncated += bool(rec.get("state_truncated"))
        rule_caps += len(rec.get("rule_input_truncated_ids") or [])
        tr = rec["trace"]
        if tr.get("jev_status") == "ok" and tr.get("jev_mode") == "single":
            raw = tr.get("jev_answers") or {}
            returned = {r[0]: r[1:] for r in tr.get("jev", [])}
            for i, (eid, version) in enumerate(tr.get("jev_pool") or []):
                mapped_candidates += 1
                p = raw.get(f"c{i}", {}).get("noul")
                q = raw.get("best", {}).get("probabilities", {}).get(f"c{i}", 0)
                actual = returned.get(eid)
                if p is None or actual is None or abs(actual[0] - p) > 1e-8 or abs(actual[1] - q) > 1e-8:
                    probability_mismatches.append({"id": rec["id"], "memory_id": eid})
    result["input_audit"] = {"collected_questions": len(recs), "fusion_replay_mismatches": replay_errors,
                              "judge_pool_distribution": dict(Counter(native_pool_sizes)),
                              "state_truncated_questions": state_truncated,
                              "rule_bodies_over_native_limit": rule_caps,
                              "full_input_probes": len(probes),
                              "full_input_probe_failures": sum(r.get("p") is None for r in probes.values()),
                              "raw_probability_candidates_checked": mapped_candidates,
                              "probability_mapping_mismatches": probability_mismatches}
    for group in ("math", "physics"):
        stage_counts, reasons, recall_details = Counter(), Counter(), Counter()
        native_vs_full = Counter()
        admission = Counter()
        for task in tasks:
            if task["group"] != group:
                continue
            rec = recs.get(task["id"])
            replay = replay_recall(rec, banks[group]) if rec else None
            for eid in task["expected_ids"]:
                stage = stage_for(eid, rec) if rec else "not_collected"
                stage_counts[stage] += 1
                detail = {"id": task["id"], "group": group, "memory_id": eid, "stage": stage,
                          "need_class": case_by_id.get(task["id"], {}).get("need_class", "unknown")}
                if stage == "judge_or_selection_rejected":
                    detail["score_reason"] = score_reason(eid, rec, policy)
                    reasons[detail["score_reason"]] += 1
                if rec:
                    before = rec["trace"].get("judge_pool_before_fit", [])
                    for count in (12, 24, 36):
                        admission[f"raw_first_{count}"] += eid in before[:count]
                    scores = {x[0]: {"p": x[1], "q": x[2]} for x in rec["trace"].get("jev", [])}
                    detail.update(scores.get(eid, {}))
                    detail["full_input_p"] = probes.get((task["id"], eid), {}).get("p")
                    detail["native_state_truncated"] = rec.get("state_truncated")
                    detail["native_rule_truncated"] = eid in rec.get("rule_input_truncated_ids", [])
                    if detail.get("p") is not None and detail["full_input_p"] is not None:
                        native_vs_full["both_scored"] += 1
                        native_vs_full["native_below_0.5_full_above"] += detail["p"] < .5 <= detail["full_input_p"]
                        native_vs_full["both_below_0.5"] += max(detail["p"], detail["full_input_p"]) < .5
                    searches = rec.get("dense_searches") or []
                    if searches:
                        detail["dense_rank"] = next((i for i, x in enumerate(searches[0]["exact_ranking"], 1) if x[0] == eid), None)
                    if replay:
                        detail["fusion_rank_before_cap"] = next((i for i, x in enumerate(replay["full_fusion_ranking"], 1) if x == eid), None)
                    if stage == "fused_recall_miss":
                        recall_details["dense_rank_outside_50"] += detail.get("dense_rank") is not None and detail["dense_rank"] > 50
                    if stage == "recall_merge_or_route_drop":
                        recall_details["fusion_rank_outside_60"] += detail.get("fusion_rank_before_cap") is not None and detail["fusion_rank_before_cap"] > 60
                details.append(detail)
        result["layers"][group] = {"gold_pairs": sum(stage_counts.values()), "stages": dict(stage_counts),
                                    "selection_score_reasons": dict(reasons), "recall_subtypes": dict(recall_details),
                                    "native_vs_full_input": dict(native_vs_full),
                                    "scan_admission_upper_bounds": dict(admission),
                                    "scan_bounds_note": "candidate visibility only; not simulated Jev selection or solver accuracy"}
        cohort = [r for r in cases if r["group"] == group]
        rescue = [r for r in cohort if r["need_class"] == "gold_rescued_both_wrong"]
        stable = [r for r in cohort if r["need_class"] in ("stable_without_memory", "stable_without_memory_gold_harmed")]
        high_conf = [r for r in cohort if r.get("confidence") is not None and r["confidence"] >= 90 and r["none1"] is not None]
        self_no = [r for r in cohort if r.get("self_needs_memory") is False and r["none1"] is not None]
        rescue_ids = {r["id"] for r in rescue}
        result["need"][group] = {
            "gold_observed_rescuable": len(rescue),
            "system_solved_rescuable": sum(r["system"] is True for r in rescue),
            "system_failed_rescuable": sum(r["system"] is False for r in rescue),
            "system_unknown_rescuable": sum(r["system"] is None for r in rescue),
            "gold_target_stages_on_rescuable_questions": dict(Counter(d["stage"] for d in details if d["group"] == group and d["id"] in rescue_ids)),
            "stable_without_memory_questions": len(stable),
            "system_injected_on_stable": sum(bool(recs.get(r["id"], {}).get("injected_ids")) for r in stable),
            "system_wrong_on_stable": sum(r["system"] is False for r in stable),
            "confidence_ge_90": len(high_conf), "confidence_ge_90_wrong": sum(r["none1"] is False for r in high_conf),
            "self_reports_no_memory": len(self_no), "self_reports_no_memory_but_wrong": sum(r["none1"] is False for r in self_no),
            "confidence_ge_90_error_95ci": wilson(sum(r["none1"] is False for r in high_conf), len(high_conf)),
        }
    for kind in ("embedding", "rerank", "jev", "solver"):
        files = list((directory / "api" / kind).glob("*.json"))
        successful, failures, usage = 0, 0, Counter()
        for p in files:
            r = json.loads(p.read_text())
            if kind == "solver":
                value = r["response"]
                ok = bool(value.get("ok"))
                for k, v in (value.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] += v
            else:
                ok = r.get("status") == 200
            successful += ok
            failures += not ok
        result["api"][kind] = {"cached_requests": len(files), "successful": successful, "failed_or_budget_exhausted": failures,
                                "tokens_reported": dict(usage)}
    dump(directory / "detailed-summary.json", result)
    (directory / "detailed-pairs.jsonl").write_text("".join(canonical(r) + "\n" for r in details))
    dump(directory / "detailed-analysis-method.json", {"code_sha256": sha(Path(__file__).read_bytes()),
                                                        "input_sha256": sha((directory / "inputs/manifest.json").read_bytes())})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
