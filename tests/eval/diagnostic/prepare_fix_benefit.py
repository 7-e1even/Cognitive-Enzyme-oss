"""Prepare paired solver directories for the frozen legacy/fixed Jev cohort.

This is an offline preparation step.  It changes only copied diagnostic inputs;
the formal retrieval records, production settings, and raw API evidence are
never modified.  The two output directories deliberately keep the stock
``benefit`` runner so that the solver prompt, grader, and cache protocol remain
the same as the existing system/closed-book evaluation.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha, verify


def _input_manifest(inputs: Path, parent_manifest: dict, variant: str) -> dict:
    files = {}
    for path in sorted(inputs.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[str(path.relative_to(inputs))] = sha(path.read_bytes())
    return {
        "protocol": "math-physics-fix-benefit-subset-v1",
        "variant": variant,
        "source_inputs_manifest_sha256": sha(canonical(parent_manifest)),
        "files": files,
        "scope": "92 questions from the frozen legacy12/fixed12 Jev comparison; no retrieval API calls",
    }


def _write_variant(source: Path, fix3: Path, destination: Path, variant: str,
                   tasks_by_id: dict, bank_data: dict, source_recs: dict,
                   result_by_id: dict, reuse_solver_cache: bool) -> dict:
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    destination.mkdir(parents=True)
    inputs = destination / "inputs"
    inputs.mkdir()
    tasks = [tasks_by_id[pid] for pid in result_by_id]
    dump(inputs / "tasks.json", tasks)
    dump(inputs / "banks.json", bank_data)
    parent_manifest = json.loads((source / "inputs/manifest.json").read_text())
    dump(inputs / "parent-manifest.json", parent_manifest)
    dump(inputs / "manifest.json", _input_manifest(inputs, parent_manifest, variant))

    rec_rows = []
    for pid in result_by_id:
        rec = json.loads(json.dumps(source_recs[pid]))
        trial = result_by_id[pid]
        rec["injected_ids"] = list(trial.get("injected_ids") or [])
        rec["selected"] = list(trial.get("selected") or [])
        rec["fix_benefit_variant"] = variant
        rec["fix_trial_sha256"] = sha((fix3 / "trials" / pid / f"{variant}12/result.json").read_bytes())
        rec_rows.append(canonical(rec))
    (destination / "retrieval.jsonl").write_text("\n".join(rec_rows) + "\n")
    source_retrieval_manifest = source / "retrieval-manifest.json"
    if source_retrieval_manifest.exists():
        shutil.copy2(source_retrieval_manifest, destination / "retrieval-manifest.json")

    # Reuse only exact, independently verified successful/budget-failure cache
    # entries.  New system answers are written locally and cannot collide with
    # the other variant.  No failed rate-limit response is reused by recovery.
    (destination / "api").mkdir()
    for kind in ("embedding", "rerank", "jev"):
        target = destination / "api" / kind
        if (source / "api" / kind).exists():
            target.symlink_to((source / "api" / kind).resolve())
    solver = destination / "api/solver"
    solver.mkdir()
    source_solver = source / "api/solver"
    for path in sorted(source_solver.glob("*.json")) if reuse_solver_cache else []:
        record = json.loads(path.read_text())
        key = record.get("key", "")
        # These are the only cache keys whose prompts are unchanged by the
        # variant.  Calibration and gold/closed-book grades can be reused.
        if key.startswith(("calibration/", "grade/", "solve/")) and any(
                key.endswith("/" + arm) for arm in ("none1", "none2", "gold")):
            (solver / path.name).symlink_to(path.resolve())
        elif key.startswith("calibration/"):
            (solver / path.name).symlink_to(path.resolve())
    dump(destination / "preparation-manifest.json", {
        "protocol": "fix-benefit-preparation-v1",
        "variant": variant,
        "question_ids": list(result_by_id),
        "source_retrieval_sha256": sha((source / "retrieval.jsonl").read_bytes()),
        "fix_results_sha256": sha((fix3 / "results.jsonl").read_bytes()),
        "reused_solver_cache_files": len(list(solver.glob("*.json"))),
        "reuse_solver_cache": reuse_solver_cache,
        "api_calls": 0,
    })
    return {"directory": str(destination), "variant": variant,
            "questions": len(tasks), "reused_solver_cache_files": len(list(solver.glob("*.json"))),
            "api_calls": 0}


def prepare(source: Path, fix3: Path, destination: Path, reuse_solver_cache: bool = True) -> dict:
    source, fix3, destination = source.resolve(), fix3.resolve(), destination.resolve()
    verify(source / "inputs")
    plan = json.loads((fix3 / "plan.json").read_text())
    task_rows = json.loads((source / "inputs/tasks.json").read_text())
    tasks_by_id = {t["id"]: t for t in task_rows}
    bank_data = json.loads((source / "inputs/banks.json").read_text())
    source_recs = {r["id"]: r for r in rows(source / "retrieval.jsonl")}
    result_rows = rows(fix3 / "results.jsonl")
    variants = {}
    for variant in ("legacy", "fixed"):
        arm = variant + "12"
        variants[variant] = {r["id"]: r for r in result_rows if r["arm"] == arm}
    question_ids = list(plan["question_ids"])
    if any(pid not in tasks_by_id or pid not in source_recs for pid in question_ids):
        raise ValueError("Fix cohort is not a subset of the frozen inputs")
    if any(len(variants[v]) != len(question_ids) for v in variants):
        raise ValueError("Fix3 result is incomplete; do not prepare a partial paired run")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")
    destination.mkdir(parents=True)
    prepared = {}
    for variant in ("legacy", "fixed"):
        prepared[variant] = _write_variant(
            source, fix3, destination / variant, variant, tasks_by_id, bank_data,
            source_recs, {pid: variants[variant][pid] for pid in question_ids}, reuse_solver_cache)
    dump(destination / "plan.json", {
        "protocol": "fix-benefit-preparation-v1",
        "source": str(source), "fix3": str(fix3), "question_ids": question_ids,
        "variants": ["legacy", "fixed"], "api_calls": 0,
        "reuse_solver_cache": reuse_solver_cache,
    })
    return prepared


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT)
    parser.add_argument("--fix3", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--no-reuse-solver-cache", action="store_true",
                        help="Do not reuse solver/grader caches from another model")
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.fix3, args.destination,
                             reuse_solver_cache=not args.no_reuse_solver_cache),
                      ensure_ascii=False, indent=2))
