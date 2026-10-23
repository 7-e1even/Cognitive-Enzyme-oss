"""Join the legacy/fixed Jev solver runs without making API calls."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import canonical, dump, rows, sha
from tests.eval.hardbench.grading import outcome


def _answers(directory: Path) -> dict[tuple[str, str], dict]:
    return {(r["id"], r["arm"]): r for r in rows(directory / "answers.jsonl")}


def _status(row: dict | None) -> str:
    value = outcome(row)
    if value is True:
        return "correct"
    if value is False:
        return "wrong"
    if row is None:
        return "missing"
    response = row.get("response") or {}
    if response.get("timed_out"):
        return "timeout"
    if row.get("error") or response.get("error"):
        return "service_or_grade_failure"
    return "ungraded"


def analyze(directory: Path) -> dict:
    directory = directory.resolve()
    plan = json.loads((directory / "plan.json").read_text())
    dirs = {v: directory / v for v in plan["variants"]}
    answers = {v: _answers(dirs[v]) for v in plan["variants"]}
    tasks = {t["id"]: t for t in json.loads((dirs["legacy"] / "inputs/tasks.json").read_text())}
    legacy_recs = {r["id"]: r for r in rows(dirs["legacy"] / "retrieval.jsonl")}
    fixed_recs = {r["id"]: r for r in rows(dirs["fixed"] / "retrieval.jsonl")}
    per_variant = {}
    cases = []
    for variant in plan["variants"]:
        data = answers[variant]
        stats = Counter()
        gold_stats = Counter()
        for pid in plan["question_ids"]:
            none = _status(data.get((pid, "none1")))
            system = _status(data.get((pid, "system")))
            gold = _status(data.get((pid, "gold")))
            stats["questions"] += 1
            stats[f"none1_{none}"] += 1
            stats[f"system_{system}"] += 1
            if none in ("correct", "wrong") and system in ("correct", "wrong"):
                stats["paired_system"] += 1
                stats["none1_correct"] += none == "correct"
                stats["system_correct"] += system == "correct"
                stats["system_rescues"] += none == "wrong" and system == "correct"
                stats["system_harms"] += none == "correct" and system == "wrong"
            if tasks[pid]["expected_ids"]:
                gold_stats["gold_questions"] += 1
                if none in ("correct", "wrong") and gold in ("correct", "wrong"):
                    gold_stats["paired_gold"] += 1
                    gold_stats["gold_rescues"] += none == "wrong" and gold == "correct"
                    gold_stats["gold_harms"] += none == "correct" and gold == "wrong"
            cases.append({"id": pid, "group": tasks[pid]["group"], "variant": variant,
                          "none1": none, "system": system, "gold": gold,
                          "injected_ids": (legacy_recs if variant == "legacy" else fixed_recs)[pid].get("injected_ids", [])})
        per_variant[variant] = {**dict(stats), **dict(gold_stats)}

    transitions = Counter()
    paired = []
    for pid in plan["question_ids"]:
        old = _status(answers["legacy"].get((pid, "system")))
        new = _status(answers["fixed"].get((pid, "system")))
        transitions[f"{old}->{new}"] += 1
        if old in ("correct", "wrong") and new in ("correct", "wrong"):
            paired.append(pid)
    injection = Counter()
    injection_rows = []
    for pid in plan["question_ids"]:
        old = set(legacy_recs[pid].get("injected_ids") or [])
        new = set(fixed_recs[pid].get("injected_ids") or [])
        kind = "same" if old == new else "changed"
        if not old and not new:
            kind = "both_empty"
        elif not old and new:
            kind = "fixed_added"
        elif old and not new:
            kind = "fixed_removed"
        injection[kind] += 1
        injection_rows.append({"id": pid, "injection_change": kind,
                               "legacy_ids": sorted(old), "fixed_ids": sorted(new),
                               "legacy_system_status": _status(answers["legacy"].get((pid, "system"))),
                               "fixed_system_status": _status(answers["fixed"].get((pid, "system")))})
    result = {
        "protocol": "fix-benefit-comparison-v1",
        "question_count": len(plan["question_ids"]),
        "variants": per_variant,
        "provider_status": {
            v: (json.loads((dirs[v] / "service-block.json").read_text())
                if (dirs[v] / "service-block.json").exists() else {"status": "not_blocked"})
            for v in plan["variants"]
        },
        "system_status_transitions": dict(transitions),
        "paired_valid_system_transitions": len(paired),
        "injection_changes": dict(injection),
        "limitations": [
            "A service/grade failure is unknown, not a wrong answer.",
            "The frozen Jev positives are partial labels; injected memory quality is not fully ground truth.",
            "This cohort isolates the Jev protection change; it does not estimate all 461 questions.",
            "The two variants use the same closed-book cache when available; missing closed-book answers remain unknown.",
        ],
        "api_calls": 0,
    }
    dump(directory / "comparison-summary.json", result)
    (directory / "comparison-cases.jsonl").write_text("".join(canonical(r) + "\n" for r in cases))
    (directory / "system-transitions.jsonl").write_text("".join(canonical(r) + "\n" for r in injection_rows))
    dump(directory / "comparison-method.json", {"code_sha256": sha(Path(__file__).read_bytes()),
                                                  "source_answers": {v: sha((dirs[v] / "answers.jsonl").read_bytes()) if (dirs[v] / "answers.jsonl").exists() else None for v in dirs},
                                                  "api_calls": 0})
    lines = ["# Jev 候选保护修复：免费模型 system/closed-book 对照", "",
             f"本报告对照 fix3 冻结的 {result['question_count']} 道题；legacy 和 fixed 使用同一套题面、同一免费模型、同一答题/判分协议，只有 system 记忆注入列表来自 legacy12 或 fixed12。closed-book 缓存可复用时两边共用；服务失败和未判分均保留为未知。", ""]
    if any(x.get("status") != "not_blocked" for x in result["provider_status"].values()):
        lines += ["当前两套目录都因免费模型返回 `Rate limit exceeded. Please try again later.` 而停止新请求；这份报告是续跑前的缓存快照，不是完整答题结论。", ""]
    for variant in ("legacy", "fixed"):
        s = per_variant[variant]
        lines += [f"## {variant}", "", "| 指标 | 数量 |", "|---|---:|",
                  f"| 题目 | {s.get('questions', 0)} |",
                  f"| closed-book 有效且答对 | {s.get('none1_correct', 0)} / {s.get('paired_system', 0)}（只在 system 也有效的配对中） |",
                  f"| system 有效且答对 | {s.get('system_correct', 0)} / {s.get('paired_system', 0)} |",
                  f"| system 救回 | {s.get('system_rescues', 0)} |",
                  f"| system 伤害 | {s.get('system_harms', 0)} |",
                  f"| closed-book 服务/未知 | {s.get('none1_service_or_grade_failure', 0) + s.get('none1_missing', 0) + s.get('none1_ungraded', 0) + s.get('none1_timeout', 0)} |",
                  f"| system 服务/未知 | {s.get('system_service_or_grade_failure', 0) + s.get('system_missing', 0) + s.get('system_ungraded', 0) + s.get('system_timeout', 0)} |", ""]
    lines += ["## fixed 相对 legacy 的 system 转移", "", "| legacy → fixed | 数量 |", "|---|---:|"]
    for key, count in sorted(transitions.items()):
        lines.append(f"| {key} | {count} |")
    lines += ["", "## Jev 注入变化", "", "| 注入列表 | 数量 |", "|---|---:|"]
    for key, count in sorted(injection.items()):
        lines.append(f"| {key} | {count} |")
    lines += ["", "## 判读", "", "只有在同一题的 closed-book、legacy system、fixed system 都有有效判分时，才能把 fixed 的答对/答错变化归因到候选保护修复。若某个状态是 service_or_grade_failure、timeout 或 missing，不能算模型答错，也不能算修复收益。若 fixed 的注入变多但 system 正确率没有同步提高，说明候选层修复只是恢复了可见性，记忆内容或模型使用方式仍需优化。", "", "逐题记录见 `comparison-cases.jsonl` 和 `system-transitions.jsonl`；两个目录中的原始答案/API 缓存可复核。"]
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.directory), ensure_ascii=False, indent=2))
