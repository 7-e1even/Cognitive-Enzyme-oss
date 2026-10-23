"""Resumable batched closed/legacy/fixed GLM answer evaluation."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import statistics
from collections import Counter
from pathlib import Path

from tests.eval.diagnostic.benefit import SELF_REPORT, render_memories
from tests.eval.diagnostic.protocol import append, dump, rows, sha
from tests.eval.diagnostic.service import blocking_service_error
from tests.eval.hardbench.grading import outcome
from tests.eval.hardbench.run import JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.diagnostic.direct_bigmodel_pilot import MODEL, request


def semantic(row):
    if not row or row.get("grade_status") != "graded" or not row.get("semantic_valid"):
        return None
    return row.get("correct") if isinstance(row.get("correct"), bool) else None


def response_secs(row):
    resp = row.get("response") or {}
    return float(resp.get("secs") or 0) + float((row.get("grader_response") or {}).get("secs") or 0)


def error_kind(row):
    """Classify provider/transport failures without treating them as wrong answers."""
    detail = str((row.get("response") or {}).get("error") or row.get("error") or "").lower()
    if not detail:
        return None
    if "timeout" in detail or "timed out" in detail:
        return "timeout"
    if any(x in detail for x in ("1302", "速率限制", "rate limit", "rate-limit", "rate_limit", "too many requests", "429")):
        return "rate_limit"
    if any(x in detail for x in ("余额不足", "无可用资源", "quota", "402")):
        return "quota"
    if any(x in detail for x in ("401", "403", "unauthorized", "forbidden")):
        return "auth"
    return "other_error"


def is_service_block(row):
    detail = str((row.get("response") or {}).get("error") or row.get("error") or "").lower()
    return any(x in detail for x in ("余额不足", "无可用资源", "rate limit", "quota", "401", "403", "429"))


async def run(args):
    token = getpass.getpass()
    root = args.directory.resolve(); root.mkdir(parents=True, exist_ok=True)
    variants = {v: root / v for v in ("legacy", "fixed")}
    for p in variants.values():
        if not (p / "inputs/tasks.json").exists():
            raise ValueError(f"Missing prepared variant directory: {p}")
    plan_path = root / "batch-plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
    else:
        source_plan = Path(args.fix3_plan).resolve()
        ids = json.loads(source_plan.read_text())["question_ids"]
        plan = {"protocol": "direct-bigmodel-batch-v1", "model": MODEL,
                "question_ids": ids, "batch_size": args.batch_size,
                "solver_thinking": args.solver_thinking, "max_tokens": args.max_tokens,
                "timeout": args.timeout, "judge_timeout": args.judge_timeout}
        dump(plan_path, plan)
    tasks = {t["id"]: t for t in json.loads((variants["legacy"] / "inputs/tasks.json").read_text())}
    banks = json.loads((variants["legacy"] / "inputs/banks.json").read_text())
    memories = {e["id"]: e for bank in banks.values() for e in bank}
    recs = {v: {r["id"]: r for r in rows(variants[v] / "retrieval.jsonl")} for v in variants}
    output = root / "answers.jsonl"
    saved = {(r["id"], r["arm"]): r for r in rows(output)}
    lock = asyncio.Lock(); semaphore = asyncio.Semaphore(args.concurrency)
    stop = asyncio.Event()

    async def api(prompt, *, max_tokens, thinking, timeout):
        async with semaphore:
            if stop.is_set():
                return {"ok": False, "error": "batch stopped by service block"}
            response = await asyncio.to_thread(request, token, prompt, max_tokens=max_tokens,
                                               thinking=thinking, timeout=timeout)
            if blocking_service_error(response):
                stop.set()
                dump(root / "service-block.json", {"error": response.get("error"),
                                                    "http": response.get("http"),
                                                    "policy": "stop new requests and preserve partial batch"})
            return response

    async def grade_answer(task, prompt, response, *, variant, arm, ids):
        row = {"id": task["id"], "group": task["group"], "variant": variant,
               "arm": arm, "model": MODEL, "injected_ids": ids,
               "request_sha256": sha(prompt), "response": response,
               "semantic_valid": False, "grade_status": "ungraded", "correct": None}
        if not response.get("ok"):
            row["grade_status"] = "timeout" if response.get("error", "").startswith("Timeout") else "service_error"
            row["error"] = response.get("error")
            return row
        text = response.get("text") or ""
        grade = grade_local(task["problem"], text)
        if grade.pop("need_judge", False):
            judge_prompt = JUDGE_PROMPT.format(domain=task["problem"]["domain"],
                                               problem=task["problem"]["problem"],
                                               answer=task["problem"]["answer"], pred=grade["pred"])
            # GLM-5.3-Flash may spend hidden reasoning tokens even when the
            # request asks for disabled thinking. 128 tokens often ends with
            # stop=length and an empty visible verdict, so keep a real budget
            # for the required final EQUIVALENT/NOT_EQUIVALENT line.
            judge = await api(judge_prompt, max_tokens=args.judge_max_tokens,
                              thinking=False, timeout=args.judge_timeout)
            verdict = parse_verdict(judge.get("text") or "") if judge.get("ok") else None
            grade.update(correct=verdict, judge_ok=verdict is not None,
                         grade_status="graded" if verdict is not None else "ungraded",
                         grader_response=judge, graded_by="judge")
            row["semantic_valid"] = verdict is not None
        else:
            grade["grade_status"] = "graded"
            row["semantic_valid"] = bool(grade.get("parsed"))
        row.update(grade)
        return row

    async def call_arm(task, variant, arm):
        key = (task["id"], arm)
        if key in saved:
            return saved[key]
        rec = recs[variant][task["id"]]
        ids = list(rec.get("injected_ids") or []) if arm in ("legacy_system", "fixed_system") else []
        block = render_memories(ids, memories, rec) if ids else ""
        prompt = (block + "\n\n" if block else "") + solve_prompt(task["problem"]) + SELF_REPORT
        response = await api(prompt, max_tokens=args.max_tokens, thinking=args.solver_thinking,
                             timeout=args.timeout)
        row = await grade_answer(task, prompt, response, variant=variant, arm=arm, ids=ids)
        async with lock:
            append(output, row); saved[key] = row
        return row

    question_ids = plan["question_ids"]
    total_batches = (len(question_ids) + args.batch_size - 1) // args.batch_size
    for batch_no, start in enumerate(range(0, len(question_ids), args.batch_size), 1):
        batch_ids = question_ids[start:start + args.batch_size]
        print(f"batch {batch_no}/{total_batches} questions={len(batch_ids)}", flush=True)
        async def process_question(pid):
            if stop.is_set(): return
            task = tasks[pid]
            # One shared closed-book answer; fixed and legacy never resample it.
            await call_arm(task, "legacy", "closed_book")
            # The two system arms are independent once the shared closed-book
            # answer exists; run them concurrently under the request semaphore.
            await asyncio.gather(
                *(call_arm(task, variant, arm)
                  for variant, arm in (("legacy", "legacy_system"), ("fixed", "fixed_system"))
                  if (pid, arm) not in saved and not stop.is_set()))
        await asyncio.gather(*(process_question(pid) for pid in batch_ids))
        dump(root / "progress.json", {"batch": batch_no, "batches": total_batches,
                                       "questions_done": min(start + len(batch_ids), len(question_ids)),
                                       "rows": len(saved), "stopped": stop.is_set()})
        if stop.is_set(): break
    summary = report(root, question_ids, saved)
    dump(root / "summary.json", summary)
    write_report(root, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def report(root, question_ids, saved):
    baseline = {}
    baseline_path = root / "answers-before-regrade.jsonl"
    if baseline_path.exists():
        baseline = {(r["id"], r["arm"]): r
                    for r in rows(baseline_path)}
    result = {"protocol": "direct-bigmodel-batch-v1", "model": MODEL,
              "planned_questions": len(question_ids), "arms": {}, "paired": {},
              "provider_cost_field": "not returned by BigModel; token usage recorded",
              "answer_rows_recorded": len(saved), "stopped": (root / "service-block.json").exists()}
    for arm in ("closed_book", "legacy_system", "fixed_system"):
        rows_arm = [saved[(pid, arm)] for pid in question_ids if (pid, arm) in saved]
        vals = [semantic(r) for r in rows_arm]
        valid = [v for v in vals if v is not None]
        responses = [row.get("response") or {} for row in rows_arm]
        usage = Counter()
        baseline_rows_arm = [baseline.get((pid, arm), saved[(pid, arm)])
                             for pid in question_ids if (pid, arm) in saved]
        retry_rows_arm = [saved[(pid, arm)] for pid in question_ids
                          if (pid, arm) in saved]
        solver_retry_count = sum(r.get("solver_retry_count", 0) for r in retry_rows_arm)
        judge_retry_count = sum(r.get("grader_retry_count", 0) for r in retry_rows_arm)
        solver_request_count = len(baseline_rows_arm) + solver_retry_count
        judge_request_count = sum(bool(r.get("grader_response")) for r in baseline_rows_arm) + judge_retry_count
        for row in baseline_rows_arm:
            resp = row.get("response") or {}
            for k, v in (resp.get("usage") or {}).items():
                if isinstance(v, (int, float)): usage[k] += v
            for k, v in ((row.get("grader_response") or {}).get("usage") or {}).items():
                if isinstance(v, (int, float)): usage["judge_" + k] += v
        for row in retry_rows_arm:
            for prefix, response_key in (("retry_solver_", "solver_retry_response"),
                                          ("retry_judge_", "grader_retry_response")):
                for k, v in ((row.get(response_key) or {}).get("usage") or {}).items():
                    if isinstance(v, (int, float)): usage[prefix + k] += v
        solver_latencies = [float(resp.get("secs") or 0) for resp in responses]
        total_latencies = [response_secs(row) for row in rows_arm]
        valid_rows = [row for row in rows_arm if semantic(row) is not None]
        valid_solver_latencies = [float((row.get("response") or {}).get("secs") or 0)
                                  for row in valid_rows]
        valid_total_latencies = [response_secs(row) for row in valid_rows]
        error_counts = Counter(error_kind(row) for row in rows_arm if error_kind(row))
        result["arms"][arm] = {
            "planned": len(question_ids), "recorded": len(rows_arm),
            "valid_graded": len(valid), "correct": sum(valid),
            "accuracy_valid": sum(valid) / len(valid) if valid else None,
            "unknown_or_failed": len(rows_arm) - len(valid),
            "timeouts": sum(r.get("grade_status") == "timeout" for r in rows_arm),
            "service_errors": sum(r.get("grade_status") == "service_error" for r in rows_arm),
            "error_kinds": dict(error_counts),
            "solver_requests": solver_request_count,
            "judge_requests": judge_request_count,
            "initial_solver_requests": len(baseline_rows_arm),
            "initial_judge_requests": sum(bool(r.get("grader_response")) for r in baseline_rows_arm),
            "retry_solver_requests": solver_retry_count,
            "retry_judge_requests": judge_retry_count,
            "total_requests": solver_request_count + judge_request_count,
            "solver_latency_p50_s": statistics.median(solver_latencies) if solver_latencies else None,
            "solver_latency_p90_s": sorted(solver_latencies)[max(0, int(.9 * len(solver_latencies)) - 1)] if solver_latencies else None,
            "latency_p50_s": statistics.median(total_latencies) if total_latencies else None,
            "latency_p90_s": sorted(total_latencies)[max(0, int(.9 * len(total_latencies)) - 1)] if total_latencies else None,
            "valid_solver_latency_p50_s": statistics.median(valid_solver_latencies) if valid_solver_latencies else None,
            "valid_solver_latency_p90_s": sorted(valid_solver_latencies)[max(0, int(.9 * len(valid_solver_latencies)) - 1)] if valid_solver_latencies else None,
            "valid_latency_p50_s": statistics.median(valid_total_latencies) if valid_total_latencies else None,
            "valid_latency_p90_s": sorted(valid_total_latencies)[max(0, int(.9 * len(valid_total_latencies)) - 1)] if valid_total_latencies else None,
            "reported_usage": dict(usage),
        }
    transitions = Counter(); valid_pairs = 0; rescue_legacy = rescue_fixed = harm_legacy = harm_fixed = 0
    pair_stats = {}
    for label, arm in (("legacy", "legacy_system"), ("fixed", "fixed_system")):
        valid = rescue = harm = 0
        pair_transitions = Counter()
        for pid in question_ids:
            c = semantic(saved.get((pid, "closed_book"))); x = semantic(saved.get((pid, arm)))
            if c is not None and x is not None:
                valid += 1; pair_transitions[f"{c}->{x}"] += 1
                rescue += int(not c and x); harm += int(c and not x)
        pair_stats[label] = {"valid_pairs": valid, "transitions": dict(pair_transitions),
                             "rescues": rescue, "harms": harm}
    for pid in question_ids:
        c = semantic(saved.get((pid, "closed_book"))); l = semantic(saved.get((pid, "legacy_system"))); f = semantic(saved.get((pid, "fixed_system")))
        if c is not None and l is not None and f is not None:
            valid_pairs += 1; transitions[f"{c}->{l}->{f}"] += 1
            rescue_legacy += (not c and l); rescue_fixed += (not c and f)
            harm_legacy += (c and not l); harm_fixed += (c and not f)
    result["paired"] = {"valid_triples": valid_pairs, "transitions": dict(transitions),
                         "legacy_rescues": rescue_legacy, "fixed_rescues": rescue_fixed,
                         "legacy_harms": harm_legacy, "fixed_harms": harm_fixed,
                         "pairwise": pair_stats}
    return result


def write_report(root, summary):
    a = summary["arms"]; p = summary["paired"]
    lines = ["# BigModel GLM：92 题 closed-book / legacy / fixed 批量对照", "",
             f"模型：`{summary['model']}`；solver thinking disabled；判官同模型、thinking disabled。计划 {summary['planned_questions']} 题。", "",
             "| 臂 | 已记录 | 有效判分 | 覆盖率 | 答对 | 有效准确率 | 求解 p50 秒 | 总延迟 p50 秒 | 总延迟 p90 秒 | 请求数 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for arm in ("closed_book", "legacy_system", "fixed_system"):
        x=a[arm]; acc="—" if x['accuracy_valid'] is None else f"{x['accuracy_valid']:.1%}"
        coverage = f"{x['valid_graded'] / x['planned']:.1%}" if x['planned'] else "—"
        lines.append(f"| {arm} | {x['recorded']} | {x['valid_graded']} | {coverage} | {x['correct']} | {acc} | {x['solver_latency_p50_s']} | {x['latency_p50_s']} | {x['latency_p90_s']} | {x['total_requests']} |")
    lines += ["", "## paired 统计", "", f"有效三联：{p['valid_triples']}；legacy 救回：{p['legacy_rescues']}；fixed 救回：{p['fixed_rescues']}；legacy 伤害：{p['legacy_harms']}；fixed 伤害：{p['fixed_harms']}。"]
    for label in ("legacy", "fixed"):
        x=p.get("pairwise", {}).get(label, {})
        lines.append(f"{label} 与 closed-book 的有效二联：{x.get('valid_pairs', 0)}；救回：{x.get('rescues', 0)}；伤害：{x.get('harms', 0)}；转移：`{x.get('transitions', {})}`。")
    lines += ["", "## 调用与失败", "", "表中延迟是全部已记录尝试；有效样本的总延迟可从 `summary.json` 的 `valid_latency_p50_s/valid_latency_p90_s` 查看。BigModel 响应没有返回货币 cost 字段，因此这里用请求次数和 input/output/reasoning token 记录调用成本；不能把 token 数直接冒充人民币成本。服务错误、超时、判官失败不计入有效准确率。"]
    for arm in ("closed_book", "legacy_system", "fixed_system"):
        x=a[arm]
        lines.append(f"- `{arm}`：solver {x['solver_requests']} 次（重试 {x.get('retry_solver_requests', 0)}），judge {x['judge_requests']} 次（重判 {x.get('retry_judge_requests', 0)}），合计 {x['total_requests']} 次；错误分类 `{x.get('error_kinds', {})}`；token `{x.get('reported_usage', {})}`。")
    total_calls = sum(x["total_requests"] for x in a.values())
    total_tokens = sum(sum(v for k, v in x.get("reported_usage", {}).items()
                           if k.endswith("total_tokens")) for x in a.values())
    lines.append(f"- 三臂合计：{total_calls} 次已记录请求，{total_tokens} 个 input/output/reasoning token（另有重试分支 5 次未落盘，见 `solver-retry-aborted.json`）。")
    lines += ["", "逐题原始记录见 `answers.jsonl`，进度见 `progress.json`。"]
    (root / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--fix3-plan", default="tests/eval/offline/data/collection-2026-10-16/two-stage-fix3/plan.json")
    p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--judge-timeout", type=float, default=60)
    p.add_argument("--judge-max-tokens", type=int, default=1024)
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--solver-thinking", action="store_true")
    asyncio.run(run(p.parse_args()))
