"""Freeze existing evaluation files and replay recorded retrieval results offline.

Only the standard library and pure metric functions are used. No model, database,
embedding client, subprocess, or network access is needed by either command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tests.eval.metrics import attribute_miss

EVAL = Path(__file__).resolve().parents[1]
ROOT = EVAL.parents[1]
DATA = Path(__file__).parent / "data"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files() -> dict[str, Path]:
    """Explicit allowlist: never copy credentials, environment files, or databases."""
    sources = {}
    hard = EVAL / "hardbench/data"
    for path in sorted(hard.glob("rsi/*")):
        if path.is_file() and path.name.startswith(("math.", "phys.")):
            group = "math" if path.name.startswith("math.") else "physics"
            sources[f"hardbench/{group}/history/{path.name}"] = path
    for path in sorted((hard / "rsi/retest_self_v3").rglob("*")):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".md", ".log"):
            rel = path.relative_to(hard / "rsi/retest_self_v3")
            sources[f"hardbench/physics/retest/{rel.as_posix()}"] = path
    extras = {"hardbench/problems.json": hard / "problems.json",
              "checks/judge_calibration.json": hard / "rsi/judge_calibration.json",
              "recall/queries.json": EVAL / "queries.json"}
    sources.update({key: path for key, path in extras.items() if path.exists()})
    for path in sorted((hard / "rsi/audit_corrected").glob("*")):
        if path.is_file() and path.suffix in (".json", ".md"):
            sources[f"checks/audit/{path.name}"] = path
    for path in sorted((EVAL / "corpus").glob("*.json")):
        sources[f"recall/corpus/{path.name}"] = path
    for path in sorted((EVAL / "reports").iterdir()):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".md"):
            sources[f"recall/history/{path.name}"] = path
    for path in sorted((EVAL / "realbench/data/results").glob("uplift_deepseek_free_diagnostic.*")):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".md", ".log"):
            sources[f"archive/realbench-diagnostic/{path.name}"] = path
    # Preserve the evaluation code that interpreted these records as well.
    for path in sorted(EVAL.rglob("*.py")):
        if "data" not in path.relative_to(EVAL).parts:
            sources[f"code/{path.relative_to(EVAL).as_posix()}"] = path
    return sources


def freeze(destination: Path) -> dict:
    if destination.exists():
        raise ValueError(f"记录已存在，不覆盖：{destination}；新快照请换目录名")
    sources = source_files()
    if not sources:
        raise ValueError("没有可保存的评测文件")
    destination.mkdir(parents=True)
    manifest = {"schema": "ce-offline-records-v1", "offline_only": True,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "limitations": [
                    "历史数学/物理答案通常没有正确记忆 ID 标签和完整召回池。缺失不等于空池或未召回。",
                    "物理库快照是 self_v3 最终状态，不代表每个历史时刻，也不代表数学库。",
                    "历史召回基准中的 math/physics 是记忆查询，与 hardbench 竞赛题分开统计。",
                    "无 API 复算只能检查已记录的候选与分数；不能预测未缓存的新 embedding/rerank/判官输出。",
                    "改变注入后的解题收益不能用旧答案推算。"],
                "files": {}}
    try:
        for key, source in sources.items():
            data = source.read_bytes()
            target = destination / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest["files"][key] = {"source": str(source.relative_to(ROOT)),
                                      "bytes": len(data), "sha256": digest(data)}
        (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        shutil.rmtree(destination)  # only the new directory owned by this invocation
        raise
    return manifest


def verify(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema") != "ce-offline-records-v1":
        raise ValueError("不支持的记录格式")
    for key, entry in manifest["files"].items():
        path = directory / key
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"记录路径越界：{key}")
        if not path.is_file() or digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError(f"记录丢失或已修改：{key}；请保留原快照，另建新版本")
    return manifest


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def retrieval_summary(rows: list[dict]) -> dict:
    """One fingerprint per input. Missing first-stage pools remain unknown."""
    good = [r for r in rows if not r.get("error") and not r.get("rerank_failed")]
    pos = [r for r in good if r.get("kind") == "positive"]
    stages, stage_sources = Counter(), Counter()
    expected_total = hits = injected_total = relevant = pool_total = pool_hit = 0
    for row in pos:
        expected, injected = set(row.get("expected", [])), set(row.get("injected", []))
        expected_total += len(expected)
        hits += len(expected & injected)
        injected_total += len(injected)
        relevant += len(injected & (expected | set(row.get("acceptable", []))))
        recalled = row.get("recalled_ids")
        if recalled is not None:
            pool_total += len(expected)
            pool_hit += len(expected & set(recalled))
        complete = all(row.get(k) is not None for k in
                       ("recalled_ids", "reranked_ids", "condition_killed_ids", "dosed"))
        judged = {x[0] for x in row.get("jev", [])} if row.get("jev_status") == "ok" else set()
        for memory in expected:
            if memory in injected:
                stage, source = "hit", "recorded_injection"
            elif complete:
                trace = {**row, "dosed_ids": row["dosed"]}
                stage, source = attribute_miss(memory, trace, list(injected)), "full_trace"
            elif memory in judged:
                stage, source = "jev_cut", "recorded_judged_pool"
            else:
                stage = row.get("attribution", {}).get(memory, "unknown")
                source = "saved_attribution" if stage != "unknown" else "missing_trace"
            stages[stage] += 1
            stage_sources[source] += 1
    return {"queries": len(rows), "valid_positive": len(pos),
            "service_errors": sum(bool(r.get("error")) for r in rows),
            "degraded": sum(bool(r.get("rerank_failed")) for r in rows),
            "expected_memories": expected_total, "hit_memories": hits,
            "final_recall": hits / expected_total if expected_total else None,
            "precision": relevant / injected_total if injected_total else None,
            "first_stage_recall": pool_hit / pool_total if pool_total else None,
            "first_stage_label_coverage": pool_total,
            "stages": dict(stages), "stage_evidence": dict(stage_sources)}


def summarize(directory: Path, domains: set[str]) -> dict:
    manifest = verify(directory)
    hard, retrieval = [], []
    for key in manifest["files"]:
        path = directory / key
        if key.startswith("hardbench/") and "/history/" in key and key.endswith(".jsonl"):
            group = key.split("/")[1]
            if ("physics" if group == "physics" else "math") not in domains:
                continue
            rows = {r["id"]: r for r in read_rows(path)}
            hard.append({"group": group, "file": key, "recorded_questions": len(rows),
                         "with_injection": sum(bool(r.get("injected")) for r in rows.values()),
                         "with_recall_trace": sum((r.get("trace") or {}).get("recalled_ids") is not None
                                                  for r in rows.values()),
                         "with_memory_labels": sum(bool(r.get("expected_memory_ids")) for r in rows.values()),
                         "first_stage_recall": None})
        if key.startswith("recall/history/") and key.endswith(".outcomes.jsonl"):
            groups = defaultdict(dict)
            for row in read_rows(path):
                if row.get("domain") in domains:
                    groups[row.get("config_fp", "legacy-unrecorded")][row["query_id"]] = row
            for fp, by_id in sorted(groups.items()):
                retrieval.append({"file": key, "config_fp": fp,
                                  **retrieval_summary(list(by_id.values()))})
    return {"schema": "ce-offline-recall-report-v1", "domains": sorted(domains),
            "saved_files": len(manifest["files"]),
            "saved_bytes": sum(f["bytes"] for f in manifest["files"].values()),
            "hardbench": hard, "labelled_retrieval": retrieval,
            "limitations": manifest["limitations"]}


def render(data: dict) -> str:
    percent = lambda x: "未知" if x is None else f"{x:.1%}"
    lines = ["# 离线召回记录", "", f"已校验 {data['saved_files']} 个文件，所有输入均来自保存的记录；本命令不调用 API。", "",
             "## 数学、物理竞赛题（hardbench）", "",
             "| 数据 | 保存题数 | 有注入 | 有召回池日志 | 有正确记忆标签 | 首段召回率 |",
             "|---|---:|---:|---:|---:|---|"]
    for row in data["hardbench"]:
        lines.append(f"| {Path(row['file']).name} | {row['recorded_questions']} | {row['with_injection']} | "
                     f"{row['with_recall_trace']} | {row['with_memory_labels']} | 未知 |")
    lines += ["", "有注入只说明系统给过记忆；没有正确记忆标签和候选池时，不能据此判断召回成功或失败。", "",
              "## 已标注的记忆召回查询（独立历史基准）", "",
              "以下仅过滤 math/physics 领域的记忆查询，不是上面的竞赛题，也不是当前代码重新跑出的成绩。",
              "首段召回率只用实际保存的 recalled_ids 复算；旧阶段归因保留来源标记。", "",
              "| 历史运行 / 指纹 | 有效查询 | 命中记忆 / 目标 | 最终召回率 | 首段召回率 |",
              "|---|---:|---:|---:|---:|"]
    for row in data["labelled_retrieval"]:
        name = Path(row["file"]).name.removesuffix(".outcomes.jsonl")
        lines.append(f"| {name} / {row['config_fp']} | {row['valid_positive']} | "
                     f"{row['hit_memories']} / {row['expected_memories']} | {percent(row['final_recall'])} | "
                     f"{percent(row['first_stage_recall'])} |")
    lines += ["", "## 使用边界", ""] + [f"- {item}" for item in data["limitations"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "report"))
    parser.add_argument("--record", type=Path, default=DATA / "2026-10-15")
    parser.add_argument("--domains", nargs="+", default=["math", "physics"])
    args = parser.parse_args()
    try:
        if args.action == "freeze":
            manifest = freeze(args.record)
            print(f"已保存 {len(manifest['files'])} 个文件：{args.record}")
        result = summarize(args.record, set(args.domains))
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    (args.record / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (args.record / "report.md").write_text(render(result))
    print(f"离线报告：{args.record / 'report.md'}")


if __name__ == "__main__":
    main()
