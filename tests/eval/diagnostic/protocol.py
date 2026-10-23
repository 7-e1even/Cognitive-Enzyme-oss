"""Frozen, provenance-filtered math/physics diagnostic inputs and pure analysis."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVAL = ROOT / "tests/eval"
DEFAULT = EVAL / "offline/data/collection-2026-10-16/formal"
RECORD = EVAL / "offline/data/2026-10-15/hardbench"
SOURCES = EVAL / "offline/data/math-physics-labels-2026-10-15/sources"
ANNOTATIONS = EVAL / "offline/annotations/math_physics.v2.json"


def sha(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rows(path: Path) -> list[dict]:
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def append(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(canonical(value) + "\n")
        f.flush()


def grams(text: str) -> set:
    words = re.findall(r"\w+", text.lower())
    return set(zip(words, words[1:], words[2:]))


def similar(a: set, b: set) -> bool:
    return bool(a and b and len(a & b) / len(a | b) >= .5)


def exclude_sources(pid: str, problems: dict, history: dict, bank: list[dict],
                    problem_grams: dict, exemplar_grams: dict) -> dict[str, list[str]]:
    target = problem_grams[pid]
    reasons = defaultdict(list)
    for other, row in history.items():
        if pid == other or similar(target, problem_grams[other]):
            for lesson in row.get("lessons") or []:
                for eid in lesson.get("ids") or []:
                    reasons[eid].append("lesson:" + other)
    for memory in bank:
        if any(similar(target, g) or " ".join(problems[pid]["problem"].split()) == " ".join(t.split())
               for t, g in exemplar_grams[memory["id"]]):
            reasons[memory["id"]].append("same_or_near_exemplar")
    return dict(reasons)


def prepare(directory: Path) -> dict:
    inputs = directory / "inputs"
    if (inputs / "manifest.json").exists():
        return verify(inputs)
    inputs.mkdir(parents=True, exist_ok=True)
    annotations = json.loads(ANNOTATIONS.read_text())
    problems = {p["id"]: p for p in json.loads((RECORD / "problems.json").read_text())}
    bad_memories = {w["memory_id"] for w in annotations["content_warnings"]}
    issues = annotations["question_issues"]
    expected = defaultdict(set)
    rationales = defaultdict(dict)
    for label in annotations["rules"]:
        if label["relation"] != "direct" or label["memory_id"] in bad_memories:
            continue
        for q in label["questions"]:
            if sha(problems[q["id"]]["problem"]) != q["problem_sha256"]:
                raise ValueError("Question hash changed: " + q["id"])
            expected[q["id"]].add(label["memory_id"])
            rationales[q["id"]][label["memory_id"]] = label["rationale"]
    tasks, banks = [], {}
    for group, catalog, relative in (
        ("math", "ce_rsi_math_self", "math/history/math.self.jsonl"),
        ("physics", "ce_rsi_phys_self_v3", "physics/history/phys.self_v3.jsonl"),
    ):
        source = SOURCES / catalog / "enzymes.jsonl"
        if sha(source.read_bytes()) != annotations["banks"][catalog]["sha256"]:
            raise ValueError("Bank hash changed: " + catalog)
        bank = rows(source)
        banks[group] = bank
        history = {r["id"]: r for r in rows(RECORD / relative)}
        pg = {pid: grams(problems[pid]["problem"]) for pid in history}
        eg = {e["id"]: [(s, grams(s)) for s in (e.get("exemplars") or []) +
                       (e.get("anti_exemplars") or []) if s.strip()] for e in bank}
        for pid, old in sorted(history.items(), key=lambda item: item[1].get("pos", 0)):
            exclusions = exclude_sources(pid, problems, history, bank, pg, eg)
            gold = sorted(expected[pid] - exclusions.keys())
            tasks.append({"id": pid, "group": group, "problem": problems[pid],
                          "excluded": exclusions, "expected_ids": gold,
                          "excluded_expected_ids": sorted(expected[pid] & exclusions.keys()),
                          "target_rationales": rationales[pid],
                          "question_issue": issues.get(pid),
                          "solve_eligible": pid not in issues,
                          "split": "calibration" if int(sha(pid)[:8], 16) % 3 == 0 else "holdout",
                          "previous_injected": old.get("injected") or []})
    dump(inputs / "tasks.json", tasks)
    dump(inputs / "banks.json", banks)
    shutil.copy2(ANNOTATIONS, inputs / "annotations.json")
    # Code is part of the run identity. Credentials and runtime configs are never copied.
    method = inputs / "method"
    code = list((ROOT / "app").rglob("*.py"))
    code += [p for p in EVAL.rglob("*.py") if "data" not in p.relative_to(EVAL).parts]
    for source in code:
        target = method / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    manifest = {
        "protocol": "math-physics-frozen-diagnostic-v1",
        "annotation_scope": "partial positives, agent reviewed; unlabelled is unknown",
        "bank_mode": "mature frozen banks; not a replay of historical learning",
        "provenance_filter": "exclude every memory touched by this/near question, plus matching exemplars; unknown lineage remains a limitation",
        "near_duplicate": "word 3-gram Jaccard >= 0.5",
        "learning_and_feedback": False,
        "relations": "not exported in source snapshot; disabled in this controlled run",
        "domains": "frozen bank domain names; descriptions unavailable",
        "counts": {g: {"questions": sum(t["group"] == g for t in tasks),
                        "solve_eligible": sum(t["group"] == g and t["solve_eligible"] for t in tasks),
                        "gold_questions": sum(t["group"] == g and t["solve_eligible"] and bool(t["expected_ids"]) for t in tasks),
                        "gold_pairs": sum(len(t["expected_ids"]) for t in tasks if t["group"] == g),
                        "memories": len(banks[g])} for g in banks},
        "files": {str(p.relative_to(inputs)): sha(p.read_bytes()) for p in sorted(inputs.rglob("*")) if p.is_file()},
    }
    dump(inputs / "manifest.json", manifest)
    return manifest


def verify(inputs: Path) -> dict:
    manifest = json.loads((inputs / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if sha((inputs / name).read_bytes()) != expected:
            raise ValueError("Frozen input changed: " + name)
    return manifest


def stage_for(eid: str, rec: dict) -> str:
    tr = rec.get("trace") or {}
    if rec.get("error"):
        return "collection_error"
    if eid in rec.get("injected_ids", []):
        return "injected"
    if eid not in tr.get("recalled_ids", []):
        dense = rec.get("dense_searches") or []
        if not dense:
            return "recall_service_or_trace_missing"
        first = dense[0]
        if eid not in first.get("indexed_ids", []):
            return "index_missing"
        exact = [x[0] for x in first.get("exact_ranking", [])]
        approximate = [x[0] for x in first.get("hits", [])]
        if eid in exact[:50] and eid not in approximate:
            return "ann_miss"
        if eid not in approximate:
            return "fused_recall_miss"
        return "recall_merge_or_route_drop"
    if eid in tr.get("condition_killed_ids", []):
        return "condition_filter"
    if tr.get("jev_status") != "ok":
        return "selector_service_failure"
    pool = {x["id"] if isinstance(x, dict) else x[0] for x in tr.get("jev_pool", [])}
    if eid not in pool:
        if eid in tr.get("judge_pool_before_fit", []):
            return "judge_budget_cut"
        return "rank_or_judge_pool_cut"
    if eid not in tr.get("dosed_ids", []):
        return "judge_or_selection_rejected"
    return "hook_filter"


def retrieval_report(directory: Path) -> dict:
    inputs = directory / "inputs"
    verify(inputs)
    tasks = json.loads((inputs / "tasks.json").read_text())
    recs = {r["id"]: r for r in rows(directory / "retrieval.jsonl")}
    pairs = []
    groups = {}
    for group in ("math", "physics"):
        gt = [t for t in tasks if t["group"] == group]
        counts = Counter()
        dense_top50 = dense_eligible = 0
        for task in gt:
            rec = recs.get(task["id"])
            for eid in task["expected_ids"]:
                stage = stage_for(eid, rec) if rec else "not_collected"
                counts[stage] += 1
                searches = (rec or {}).get("dense_searches") or []
                rank = next((i for i, x in enumerate(searches[0].get("exact_ranking", []), 1) if x[0] == eid), None) if searches else None
                if rank is not None:
                    dense_eligible += 1
                    dense_top50 += rank <= 50
                pairs.append({"id": task["id"], "group": group, "memory_id": eid,
                              "stage": stage, "dense_exact_rank": rank,
                              "question_issue": task["question_issue"]})
        groups[group] = {"questions": len(gt), "collected": sum(t["id"] in recs for t in gt),
                         "errors": sum(bool(recs.get(t["id"], {}).get("error")) for t in gt),
                         "gold_pairs": sum(counts.values()), "stages": dict(counts),
                         "dense_top50": dense_top50, "dense_ranked_gold": dense_eligible,
                         "injected_questions": sum(bool(recs.get(t["id"], {}).get("injected_ids")) for t in gt)}
    dump(directory / "retrieval-summary.json", groups)
    path = directory / "retrieval-pairs.jsonl"
    path.write_text("".join(canonical(r) + "\n" for r in pairs))
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "report"))
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    args = parser.parse_args()
    value = prepare(args.directory) if args.stage == "prepare" else retrieval_report(args.directory)
    print(json.dumps(value.get("counts", value), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
