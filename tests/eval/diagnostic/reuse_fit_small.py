"""Small Jev validation for reusing the incumbent fit_pool before overflow scans.

The frozen candidates and the existing scan12 results are reused as the
baseline. This experiment only calls Jev for the 20-question small cohort;
embedding, rerank, retrieval, solving, learning, and production writes stay
out of the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.protocol import append, canonical, dump, rows, sha
from tests.eval.diagnostic import two_stage as ts

BASE = Path("tests/eval/offline/data/collection-2026-10-16/two-stage-small")
DEFAULT = BASE.parent / "two-stage-reuse-fit-small"


def prepare(source: Path, directory: Path):
    source, directory = source.resolve(), directory.resolve()
    if directory.exists():
        raise ValueError("Use a new experiment directory")
    shutil.copytree(source / "inputs", directory / "inputs")
    plan = json.loads((source / "plan.json").read_text())
    plan.update({
        "protocol": "frozen-candidate-reuse-fit-small-v1",
        "source_plan": str(source / "plan.json"),
        "baseline_results": str(source / "results.jsonl"),
        "reuse_fit": True,
        "experiment_arm": "reuse12",
        "core_sha256": ts.core_hashes(),
        "runner_sha256": sha(Path(__file__).read_bytes()),
    })
    directory.mkdir(parents=True, exist_ok=True)
    dump(directory / "plan.json", plan)
    return {"directory": str(directory), "questions": len(plan["question_ids"]),
            "baseline": str(source / "results.jsonl"), "api_calls": 0}


def load(directory: Path):
    plan = json.loads((directory / "plan.json").read_text())
    if ts.core_hashes() != plan["core_sha256"]:
        raise ValueError("Matching source changed after experiment preparation")
    tasks = {t["id"]: t for t in json.loads((directory / "inputs/tasks.json").read_text())}
    recs = {r["id"]: r for r in rows(directory / "inputs/retrieval.jsonl")}
    banks = json.loads((directory / "inputs/banks.json").read_text())
    metadata = json.loads((directory / "inputs/metadata.json").read_text())
    return plan, tasks, recs, banks, metadata


async def run(directory: Path):
    from app import settings
    from app.matching import pipeline as pl
    import app.typesafe_client as tc
    from tests.eval.diagnostic.collect import configuration

    directory = directory.resolve()
    plan, tasks, recs, banks, metadata = load(directory)
    config, registry, _ = await configuration()
    if config.typesafe.model != metadata["models"]["jev"]:
        raise ValueError("Configured Jev model changed")
    settings._values.clear()
    settings._values.update(metadata["policy"])
    completed = {(r["id"], r["arm"]) for r in rows(directory / "results.jsonl")}
    ordered = sorted(plan["question_ids"], key=lambda pid: sha("reuse-fit-small-order-v1:" + pid))
    consecutive_service_failures = 0
    for index, pid in enumerate(ordered):
        if (pid, "reuse12") in completed:
            continue
        task, rec = tasks[pid], recs[pid]
        restored = ts.restore(task, rec, banks)
        trialdir = directory / "trials" / pid / "reuse12"
        trialdir.mkdir(parents=True, exist_ok=False)
        http = ts.FreshHTTP(trialdir, [p.api_key for p in registry.providers.values()])
        tc._client = http
        settings._values["judge.two_stage"] = True
        settings._values["judge.scan_reuse_fit"] = True
        settings._values["judge.scan_pool"] = 12
        trace = {}
        started = time.perf_counter()
        judges = pl._start_judges(task["problem"]["problem"], restored["pool"],
                                  config.typesafe, None, restored["analogies"])
        try:
            app, _ = await pl._collect_judges(judges[0], judges[1], judges[2], trace, None,
                                              task["problem"]["problem"], judges[3])
        finally:
            pending = [t for t in judges[:2] if t is not None and not t.done()]
            for pending_task in pending:
                pending_task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        elapsed = time.perf_counter() - started
        selected, injected = ts.select(app, restored, metadata["max_results"])
        record = {
            "id": pid, "group": task["group"], "arm": "reuse12", "judge_ok": app is not None,
            "jev_stage_s": round(elapsed, 6), "wire_attempts": len(http.records),
            "wire_statuses": dict(Counter(str(r["status"]) for r in http.records)),
            "peak_http_concurrency": http.peak, "logical_calls": app.calls if app else None,
            "injected_ids": injected, "selected_ids": [r.enzyme.id for r in selected],
            "historical_injected_ids": rec["injected_ids"], "native_pool_ids": [e for e, _ in rec["trace"]["jev_pool"]],
            "raw_pool_ids": [e.id for e in restored["pool"]], "p": app.p if app else {},
            "q": app.q if app else {}, "none": app.none if app else None,
            "scan_ids": ts.scan_ids(restored, 12), "application": __import__("dataclasses").asdict(app) if app else None,
            "trace": trace,
        }
        dump(trialdir / "result.json", record)
        append(directory / "results.jsonl", record)
        completed.add((pid, "reuse12"))
        await http.client.aclose()
        failed = app is None and any(r.get("status") in (401, 402, 403, 429, 500, 502, 503)
                                     for r in http.records)
        consecutive_service_failures = consecutive_service_failures + 1 if failed else 0
        print(f"trial {index + 1}/{len(ordered)} {pid} reuse12 ok={app is not None} "
              f"s={elapsed:.3f} wire={len(http.records)}", flush=True)
        if http.auth_block or consecutive_service_failures >= 2:
            dump(directory / "service-block.json", {"reason": "provider rejection; stop validation"})
            break
    tc._client = None
    return report(directory)


def quantile(values, q):
    if not values:
        return None
    values = sorted(values)
    rank = (len(values) - 1) * q
    lo, hi = int(rank), int(rank + 0.999999)
    return values[lo] + (values[hi] - values[lo]) * (rank - lo)


def arm_stats(records, plan):
    target = plan["target_pairs"]
    measured = [r for r in records.values()]
    complete = [p for p in target if (p["id"], "reuse12") in records]
    return {
        "measured_questions": len(measured),
        "judge_successes": sum(r["judge_ok"] for r in measured),
        "target_pairs_measured": len(complete),
        "target_scored": sum(p["memory_id"] in records[p["id"], "reuse12"]["p"] for p in complete),
        "target_injected": sum(p["memory_id"] in records[p["id"], "reuse12"]["injected_ids"] for p in complete),
        "latency_p50_s": quantile([r["jev_stage_s"] for r in measured], .5),
        "latency_p90_s": quantile([r["jev_stage_s"] for r in measured], .9),
        "wire_attempts": sum(r["wire_attempts"] for r in measured),
        "wire_per_question": sum(r["wire_attempts"] for r in measured) / len(measured) if measured else None,
        "all_injected": sum(len(r["injected_ids"]) for r in measured),
    }


def report(directory: Path):
    directory = directory.resolve()
    plan, *_ = load(directory)
    current = {(r["id"], r["arm"]): r for r in rows(directory / "results.jsonl")}
    baseline_path = Path(plan["baseline_results"])
    baseline = {(r["id"], r["arm"]): r for r in rows(baseline_path)}
    reuse = {(pid, "reuse12"): r for (pid, arm), r in current.items() if arm == "reuse12"}
    stats = arm_stats(reuse, plan)
    base_rows = {(pid, "scan12"): r for pid in plan["question_ids"]
                 for r in [baseline.get((pid, "scan12"))] if r}
    paired = [(base_rows[(pid, "scan12")], reuse[(pid, "reuse12")])
              for pid in plan["question_ids"] if (pid, "scan12") in base_rows and (pid, "reuse12") in reuse]
    controls = []
    for pid in plan["control_question_ids"]:
        old, new = base_rows.get((pid, "scan12")), reuse.get((pid, "reuse12"))
        if not old or not new:
            continue
        positives = set(next(t["expected_ids"] for t in json.loads((directory / "inputs/tasks.json").read_text())
                             if t["id"] == pid))
        old_pos = positives & set(old["injected_ids"])
        new_pos = positives & set(new["injected_ids"])
        controls.append({"id": pid, "old": sorted(old_pos), "new": sorted(new_pos),
                         "lost": sorted(old_pos - new_pos), "gained": sorted(new_pos - old_pos)})
    target_changes = Counter()
    for pair in plan["target_pairs"]:
        old, new = base_rows.get((pair["id"], "scan12")), reuse.get((pair["id"], "reuse12"))
        if not old or not new:
            continue
        old_hit = pair["memory_id"] in old["injected_ids"]
        new_hit = pair["memory_id"] in new["injected_ids"]
        target_changes[f"{old_hit}->{new_hit}"] += 1
    result = {
        "protocol": "frozen-candidate-reuse-fit-small-v1",
        "baseline": str(baseline_path), "arm": "reuse12", "complete": len(reuse) == len(plan["question_ids"]),
        "reuse": stats, "paired_questions": len(paired),
        "paired_latency_delta_p50_s": quantile([b["jev_stage_s"] - a["jev_stage_s"] for a, b in paired], .5),
        "paired_wire_delta_per_question": (sum(b["wire_attempts"] - a["wire_attempts"] for a, b in paired) / len(paired)
                                           if paired else None),
        "target_injection_transitions": dict(target_changes), "controls": controls,
        "embedding_calls": 0, "rerank_calls": 0, "solver_calls": 0, "production_writes": 0,
    }
    dump(directory / "summary.json", result)
    lines = ["# 复用原判定池的小范围预算验证", "",
             "固定同一冻结候选、同一 scan12、同一 Jev 与选择阈值；只把原 fit_pool 的联合判定结果复用，逐条扫描预算只给预算外候选。embedding、rerank、解题、学习和生产写入均为 0。", "",
             f"有效完成：{stats['measured_questions']}/{len(plan['question_ids'])}；判定成功：{stats['judge_successes']}；目标正例注入：{stats['target_injected']}/{stats['target_pairs_measured']}；延迟 p50/p90：{stats['latency_p50_s']} / {stats['latency_p90_s']} 秒；HTTP/题：{stats['wire_per_question']}", "",
             f"相对已有 scan12 基线，配对题数 {len(paired)}，延迟中位增量 {result['paired_latency_delta_p50_s']} 秒，HTTP/题增量 {result['paired_wire_delta_per_question']}；目标正例转移：`{dict(target_changes)}`。", "",
             f"原已注入控制：{sum(bool(c['old']) for c in controls)} 题有正例；丢失 {sum(bool(c['lost']) for c in controls)}，新增 {sum(bool(c['gained']) for c in controls)}。", "",
             "这只是预算/选择路径的小样本验证，不改变全局开关；只有在目标正例保留、延迟和服务失败都可接受后，才考虑扩大验证。", "",
             "详细试次见 `trials/`，汇总见 `summary.json`。"]
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "run", "report"))
    parser.add_argument("--source", type=Path, default=BASE)
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.source, args.directory)
    elif args.stage == "run":
        result = asyncio.run(run(args.directory))
    else:
        result = report(args.directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))
