"""Prepare a separate recovery directory, offline; never retry successful samples."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.benefit import job_arms
from tests.eval.diagnostic.protocol import DEFAULT, canonical, dump, rows, sha, verify


def reusable(response):
    if response.get("input_delivery_verified") is False or response.get("harness_error"):
        return False
    return (response.get("ok") and response.get("input_delivery_verified") is True
            or response.get("timed_out") is True
            or response.get("protocol_error") == "tool_attempt")


def prepare(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    verify(source / "inputs")
    if destination.exists():
        raise ValueError("Recovery destination must be new; source evidence is never modified")
    destination.mkdir(parents=True)
    for name in ("inputs", "retrieval.jsonl", "retrieval-manifest.json", "gold-full-input.jsonl",
                 "transport-health.json", "content-review.json"):
        if (source / name).exists():
            (destination / name).symlink_to((source / name).resolve())
    (destination / "api").mkdir()
    for kind in ("embedding", "rerank", "jev"):
        (destination / "api" / kind).symlink_to((source / "api" / kind).resolve())
    solver = destination / "api/solver"
    solver.mkdir()
    kept, discarded = [], []
    for path in sorted((source / "api/solver").glob("*.json")):
        record = json.loads(path.read_text())
        if reusable(record["response"]):
            (solver / path.name).symlink_to(path.resolve())
            kept.append({"key": record["key"], "sha256": sha(path.read_bytes())})
        else:
            discarded.append({"key": record["key"], "error": record["response"].get("error"),
                              "sha256": sha(path.read_bytes())})
    quality = [r for r in rows(source / "question-quality.jsonl")
               if not r.get("audit_error") and r["completeness"] != "uncertain"]
    (destination / "question-quality.jsonl").write_text("".join(canonical(r) + "\n" for r in quality))
    tasks = json.loads((source / "inputs/tasks.json").read_text())
    recs = {r["id"]: r for r in rows(source / "retrieval.jsonl")}
    kept_keys = {r["key"] for r in kept}
    pending = []
    for task in tasks:
        if task["solve_eligible"]:
            for arm in job_arms(task, recs[task["id"]]):
                key = f"solve/{task['id']}/{arm}"
                if key not in kept_keys:
                    pending.append({"id": task["id"], "group": task["group"], "arm": arm,
                                    "split": task["split"], "has_gold": bool(task["expected_ids"])})
    dump(destination / "pending-solves.json", pending)
    manifest = {"source": str(source), "preparation_only": True, "api_calls": 0,
                "source_manifests": {n: sha((source / n).read_bytes()) for n in
                                     ("baseline-manifest.json", "benefit-manifest.json", "quality-manifest.json")},
                "input_sha256": sha((source / "inputs/manifest.json").read_bytes()),
                "reused": kept, "not_reused": discarded, "pending_solves": len(pending),
                "pending_by_arm": dict(Counter(r["arm"] for r in pending)),
                "policy": "reuse exact-input successful responses and recorded budget failures; collect only missing/service-failed requests after provider access is restored",
                "code_sha256": sha(Path(__file__).read_bytes())}
    dump(destination / "recovery-manifest.json", manifest)
    return {"directory": str(destination), "reused_cache_entries": len(kept),
            "not_reused_cache_entries": len(discarded), "pending_solves": len(pending),
            "pending_by_arm": manifest["pending_by_arm"], "api_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source, args.destination), ensure_ascii=False, indent=2))
