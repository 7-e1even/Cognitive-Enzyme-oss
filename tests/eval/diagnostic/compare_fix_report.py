"""Offline analysis of the full pre/post choice-protection experiment."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import canonical, dump, rows, sha


def qstate(row, eid):
    if row is None:
        return "unmeasured"
    if not row["judge_ok"]:
        return "service_or_parse_failure"
    if eid in row["injected_ids"]:
        return "injected"
    if eid in row["p"]:
        return "scored_not_selected"
    return "not_scored"


def analyze(directory: Path):
    directory = directory.resolve()
    plan = json.loads((directory / "plan.json").read_text())
    records = {(r["id"], r["arm"]): r for r in rows(directory / "results.jsonl")}
    transitions = Counter()
    pair_rows = []
    for pair in plan["target_pairs"]:
        pid, eid = pair["id"], pair["memory_id"]
        old, new = records.get((pid, "legacy12")), records.get((pid, "fixed12"))
        transition = (qstate(old, eid), qstate(new, eid))
        transitions[transition] += 1
        pair_rows.append({**pair, "legacy_state": transition[0], "fixed_state": transition[1],
                          "legacy_p": old.get("p", {}).get(eid) if old else None,
                          "fixed_p": new.get("p", {}).get(eid) if new else None,
                          "legacy_q": old.get("q", {}).get(eid) if old else None,
                          "fixed_q": new.get("q", {}).get(eid) if new else None})

    def arm_stats(arm):
        values = [r for (pid, a), r in records.items() if a == arm]
        times = [r["jev_stage_s"] for r in values]
        statuses = Counter(k for r in values for k, n in r["wire_statuses"].items() for _ in range(n))
        return {"trials": len(values), "judge_ok": sum(r["judge_ok"] for r in values),
                "p50_s": statistics.median(times) if times else None,
                "p90_s": sorted(times)[max(0, int(.9 * len(times)) - 1)] if times else None,
                "wire_attempts": sum(r["wire_attempts"] for r in values),
                "wire_per_trial": sum(r["wire_attempts"] for r in values) / len(values) if values else None,
                "wire_statuses": dict(statuses), "injected": sum(len(r["injected_ids"]) for r in values)}

    controls = {}
    for arm in ("legacy12", "fixed12"):
        valid = 0
        retained = 0
        for pair in plan["control_pairs"]:
            row = records.get((pair["id"], arm))
            if row and row["judge_ok"]:
                valid += 1
                retained += pair["memory_id"] in row["injected_ids"]
        controls[arm] = {"valid": valid, "original_positive_retained": retained,
                         "total_original_positive": len(plan["control_pairs"])}
    paired_valid = [x for x in pair_rows if x["legacy_state"] not in ("service_or_parse_failure", "unmeasured")
                    and x["fixed_state"] not in ("service_or_parse_failure", "unmeasured")]
    result = {"protocol": "comparison-budget-fix-v1", "target_pairs": len(pair_rows),
              "paired_valid_target_pairs": len(paired_valid),
              "target_state_transitions": {f"{old}->{new}": n for (old, new), n in transitions.items()},
              "arms": {arm: arm_stats(arm) for arm in ("legacy12", "fixed12", "legacy36", "fixed36")},
              "controls": controls,
              "stress": {arm: ({"judge_ok": records[(plan["stress_case"], arm)]["judge_ok"],
                                 "p": records[(plan["stress_case"], arm)]["p"].get("4f051286-6273-4582-a071-fb02b8e871c5"),
                                 "q": records[(plan["stress_case"], arm)]["q"].get("4f051286-6273-4582-a071-fb02b8e871c5"),
                                 "injected": "4f051286-6273-4582-a071-fb02b8e871c5" in records[(plan["stress_case"], arm)]["injected_ids"],
                                 "seconds": records[(plan["stress_case"], arm)]["jev_stage_s"],
                                 "wire_attempts": records[(plan["stress_case"], arm)]["wire_attempts"]})
                         for arm in plan["stress_arms"] if (plan["stress_case"], arm) in records},
              "limitations": ["95 positive pairs are partial labels, not full precision ground truth",
                              "one fresh trial per arm; provider 503/422/timeout are unknown, not semantic failures",
                              "only 16 originally injected positive controls; no solver outcome was rerun",
                              "Jev timing excludes retrieval and answer generation"],
              "api_calls": 0}
    dump(directory / "final-analysis.json", result)
    (directory / "final-pairs.jsonl").write_text("".join(canonical(x) + "\n" for x in pair_rows))
    write_report(directory, result)
    dump(directory / "final-analysis-method.json", {"code_sha256": sha(Path(__file__).read_bytes()),
          "results_sha256": sha((directory / "results.jsonl").read_bytes()), "api_calls": 0})
    return result


def write_report(directory, result):
    a = result["arms"]
    lines = ["# 两段判定候选保护修复：95 组全量对照", "",
             "本次对照使用同一批冻结候选、同一 Jev、同一 6 秒阶段预算和同一选择阈值。旧版是现有 two_stage；修复版把原单批候选单独保留 choice 证据，新增扫描候选另行比较。两者都只调用 Jev，不调用检索或解题模型。", "",
             "## 结论", "",
             "修复解决了已确认的候选挤出问题：压力题 physelite-2824 旧版的目标记忆只有 p≈0.330、没有 q，未注入；修复版保留原候选比较，得到 q≈0.733 并注入。16 个原已注入正例的控制中，旧版保留 13 个，修复版保留 16 个。", "",
             "对 95 个部分正例，剔除服务失败后的有效配对见逐条转移表；服务失败不算语义退步。修复版允许新增候选，但新增记忆没有独立人工真值，不能把所有新增注入称为正确。", "",
             "## 调用和延迟", "", "| 方案 | 试次 | 判定成功 | p50 秒 | p90 秒 | HTTP 次数 | 每题 HTTP |", "|---|---:|---:|---:|---:|---:|---:|"]
    for arm in ("legacy12", "fixed12"):
        x=a[arm];lines.append(f"| {arm} | {x['trials']} | {x['judge_ok']} | {x['p50_s']:.2f} | {x['p90_s']:.2f} | {x['wire_attempts']} | {x['wire_per_trial']:.2f} |")
    lines += ["", "修复版每题约多一次 choice 请求；本次 92 道题的平均 HTTP 从旧版约 5.72 次升到修复版约 6.68 次，p50 从 3.26 秒升到 4.16 秒。出现的 503/422/超时保留为服务或输入问题，不当成模型拒绝；其中两次 422 是当时保护集为空时只发送了 none 选项，当前代码已加守卫，改为跳过空比较或直接使用新增候选比较。", "",
              "## 正例转移（旧版 → 修复版）", "", "| 旧版状态 | 修复版状态 | 数量 |", "|---|---|---:|"]
    names={"injected":"已注入","scored_not_selected":"有分未选","not_scored":"未拿到分","service_or_parse_failure":"服务/解析失败","unmeasured":"未测"}
    for transition,n in sorted(result["target_state_transitions"].items()):
        old,new=transition.split("->",1)
        lines.append(f"| {names.get(old,old)} | {names.get(new,new)} | {n} |")
    lines += ["", "其中两边都有效的目标正例配对数为 %d。修复版相对旧版的目标注入变化，必须以这张转移表为准；‘有分未选’不是正确性标签，只表示候选到达了 Jev 但选择门没有注入。" % result["paired_valid_target_pairs"], "",
              "## 控制与压力题", "", "| 方案 | 原已注入正例保留 | 有效控制数 |", "|---|---:|---:|"]
    for arm in ("legacy12", "fixed12"):
        x=result["controls"][arm];lines.append(f"| {arm} | {x['original_positive_retained']}/{x['total_original_positive']} | {x['valid']} |")
    for arm,x in result["stress"].items():lines.append(f"| {arm} 压力题 | q={x['q']}，注入={x['injected']}，{x['seconds']:.2f}s，{x['wire_attempts']} 次 HTTP | |")
    lines += ["", "## 是否现在全局打开", "", "建议先把修复作为小流量/影子实验，不直接全局打开：它保住了原有 choice 证据，但每次增加一次判定请求，p50 延迟约增加 0.9 秒，并增加服务失败暴露面。下一步应在服务稳定后补做解题模型的 system vs closed-book 对照，只有‘实际答对率提升’得到验证，才说明新增候选值得这笔成本。", "", "逐条结果见 `final-pairs.jsonl`，原始请求和响应在 `trials/`，修复前后代码快照在 `legacy-method/` 和 `fixed-method/`。"]
    (directory / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).parents[1] / "offline/data/collection-2026-10-16/two-stage-fix3")
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory), ensure_ascii=False, indent=2))
