# tests/eval/run_eval.py
"""端到端召回评测:进程内直调 Engine.match,复刻 hook 过滤,产出指标报表。

用法:
  python -m tests.eval.run_eval --name baseline          # 全量评测
  python -m tests.eval.run_eval --name x --limit 5       # 冒烟
  python -m tests.eval.run_eval --compare baseline knobA # 两份报表 diff

断点续跑:每条查询算完就追加一行 JSON 到 reports/<name>.outcomes.jsonl。
同名重跑时会先加载该文件里已完成的 query_id 并跳过,只补算剩余的——
用于长跑(~130 条查询,10-25 分钟)被中断后无需从头重来。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import time
from pathlib import Path

from tests.eval.metrics import QueryOutcome, attribute_miss, evaluate

EVAL_DIR = Path(__file__).parent
REPORTS_DIR = EVAL_DIR / "reports"

HOOK_MIN_CONF = float(os.environ.get("CE_EVAL_HOOK_MIN_CONF", "0.25"))
MAX_RESULTS = 6          # 对齐 hook CE_PRIME_MAX 默认
ERROR_ABORT_RATE = 0.05


def _load_done(outcomes_path: Path, fp: str) -> dict[str, QueryOutcome]:
    """加载已完成的 outcomes(按 query_id 去重,后写覆盖前写)。

    只复用 config_fp 与本次运行相同的行;不同配置指纹(含缺失该字段的旧行,
    例如未打指纹前写入的 baseline.outcomes.jsonl)一律视为过期缓存丢弃,
    避免不同旋钮下的结果混入同一份报表。
    """
    done: dict[str, QueryOutcome] = {}
    if not outcomes_path.exists():
        return done
    stale_fps: dict[str, int] = {}
    known_fields = {f.name for f in dataclasses.fields(QueryOutcome)}
    for line in outcomes_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        if data.get("config_fp") != fp:
            stale_fps[data.get("config_fp")] = stale_fps.get(data.get("config_fp"), 0) + 1
            continue
        outcome = QueryOutcome(**{k: v for k, v in data.items() if k in known_fields})
        if outcome.error or outcome.rerank_failed:
            done.pop(data["query_id"], None)   # 出错 / 精排降级的行不算完成,续跑时重做
            continue
        done[data["query_id"]] = outcome
    for stale_fp, n in stale_fps.items():
        print(f"[resume] 丢弃 {n} 条旧配置(fp={stale_fp})的缓存结果")
    return done


async def _record_gold(engine, decision_id: str, q: dict) -> None:
    """把标准答案挂到这次判定的记录上(判定记录平台:评测也产出带真实标签的训练数据)。"""
    if not decision_id:
        return
    for gid in q["expected_ids"]:
        await engine.record_outcome(gid, "gold", "eval", decision_id=decision_id)
    for aid in q.get("acceptable_ids", []):
        await engine.record_outcome(aid, "acceptable", "eval", decision_id=decision_id)
    if q["kind"] == "silent":
        await engine.record_outcome("*", "silent", "eval", decision_id=decision_id)


async def _evaluate_query(engine, q: dict) -> QueryOutcome:
    trace: dict = {}
    try:
        t0 = time.perf_counter()
        results = await engine.match(q["text"], max_results=MAX_RESULTS, trace=trace, source="eval")
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        await _record_gold(engine, trace.get("decision_id", ""), q)
        # 镜像 hook:置信度过滤,但探索项(exploratory)豁免——与 ce_prime_hook 一致
        injected = [r.enzyme.id for r in results
                    if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory]
        attribution = {eid: attribute_miss(eid, trace, injected)
                       for eid in q["expected_ids"]}
        return QueryOutcome(
            q["id"], q["kind"], q["domain"], q["expected_ids"],
            q["acceptable_ids"], injected, attribution,
            # 落盘逐查询明细:此前 trace 用完即弃,变k剂量/floor 重扫的
            # 离线模拟因此做不了(2026-07-07 验证结论)。分数 round 到 6 位
            # 控报表体积。
            scored=[[eid, round(s, 6), tier]
                    for eid, s, tier in trace.get("scored", [])],
            dosed=list(trace.get("dosed_ids", [])),
            rerank_raw=[[eid, round(s, 6)]
                        for eid, s in trace.get("rerank_raw", [])],
            jev=[[eid, round(p, 4), round(qv, 4)] for eid, p, qv in trace.get("jev", [])],
            jev_none=trace.get("jev_none"),
            jev_status=trace.get("jev_status", ""),
            jev_wait_ms=trace.get("jev_wait_ms"),
            llm_judge_status=trace.get("llm_judge_status", ""),
            expansion=trace.get("expansion"),
            rerank_failed=bool(trace.get("rerank_failed")),
            recalled_ids=trace.get("recalled_ids"),
            reranked_ids=trace.get("reranked_ids"),
            condition_killed_ids=trace.get("condition_killed_ids"),
            latency_ms=latency_ms)
    except Exception as exc:  # 网络类失败单列,不污染指标
        return QueryOutcome(
            q["id"], q["kind"], q["domain"], q["expected_ids"],
            q["acceptable_ids"], [], {}, error=f"{type(exc).__name__}: {exc}")


async def run(name: str, limit: int | None, require_resume: bool = False, concurrency: int = 1) -> None:
    from app.core.engine import Engine
    import app.matching.pipeline as pipeline
    from app.matching.expand import expand_on
    from app.matching.lexical import lexical_on
    from tests.eval.bootstrap import eval_stores
    from tests.eval.validate import CORPUS_DIR, QUERIES_PATH, load_corpus, load_queries

    pipeline._EXPLORATION_RATE = 0.0  # 评测必须确定性

    config, pg, qdrant = await eval_stores()
    engine = Engine(config, pg, qdrant)
    enzymes = load_corpus(CORPUS_DIR)
    queries = load_queries(QUERIES_PATH, {e.id for e in enzymes})
    if limit:
        queries = queries[:limit]

    config_snapshot = {
        "hook_min_conf": HOOK_MIN_CONF,
        "max_results": MAX_RESULTS,
        # 这三个旋钮在 Task 9 才接线;在那之前改这些 env 不影响行为,快照仅为前瞻
        "search_query_mode": os.environ.get("CE_SEARCH_QUERY_MODE", "full"),
        "rerank_doc_mode": os.environ.get("CE_RERANK_DOC_MODE", "patterns"),
        "scorer_w_relevance": float(os.environ.get("CE_W_RELEVANCE", "0.55")),
        # Task 9b 旋钮:入指纹,防断点续跑混入不同配置的旧行
        "conditions_mode": os.environ.get("CE_CONDITIONS_MODE", "soft"),
        "relevance_floor": os.environ.get("CE_RELEVANCE_FLOOR", "0.01"),
        "rerank_topn_mult": os.environ.get("CE_RERANK_TOPN_MULT", "2"),
        # Task 9c (round-G) 旋钮:同样入指纹
        "dosage_budget": os.environ.get("CE_DOSAGE_BUDGET", "5"),
        "conditions_boost_stage": os.environ.get("CE_CONDITIONS_BOOST_STAGE", "pattern"),
        "embed_model": config.embedding.model,
        "rerank_model": config.embedding.rerank_model,
        # Stage 4.5(Jev):未配置 key 时整段不生效,快照记 "off" 以免与开启态混指纹
        "jev": ({k: os.environ.get(k, "") for k in (
                    "CE_JEV_MODE", "CE_JEV_STAGE", "CE_JEV_POOL", "CE_JEV_TIMEOUT",
                    "CE_JEV_CHOICE_MIN", "CE_JEV_PRIMARY_P", "CE_JEV_SUPPLEMENT_P",
                    "CE_JEV_NONE_MAX", "CE_JEV_CRITICAL_P", "CE_JEV_PROMOTE_BELOW",
                    "CE_JEV_PROMOTE_P", "CE_JEV_RESCUE", "CE_JEV_RESCUE_P", "CE_JUDGE_LLM",
                    "CE_JUDGE_LLM_MODEL", "CE_JUDGE_LLM_EXTRA", "CE_JEV_PROMPT", "CE_EXEMPLAR_RECALL",
                    "CE_EXEMPLAR_HINT", "CE_ANTI_EXEMPLAR", "CE_JEV_RESCUE_Q", "CE_JEV_CHOICE_OVERRIDE",
                    "CE_EXEMPLAR_ANALOGY", "CE_ANALOGY_Q",
                    "CE_ANALOGY_P", "CE_ANALOGY_K")}
                 | {"model": config.typesafe.model,
                    "llm": config.llm.model if config.llm.available else ""}
                if config.typesafe.available and os.environ.get("CE_JEV_MODE", "on") != "off"
                else "off"),
        # 词面召回(BM25,召回阶段):未开启记 "off"
        "lexical": ({"limit": os.environ.get("CE_LEXICAL_LIMIT", ""), "avgdl": os.environ.get("CE_LEXICAL_AVGDL", "")}
                    if lexical_on() else "off"),
        # 查询扩写(召回阶段,与 Jev 无关):未开启记 "off"
        "query_expand": ({k: os.environ.get(k, "") for k in (
                              "CE_QUERY_EXPAND_MODEL", "CE_QUERY_EXPAND_MERGE", "CE_QUERY_EXPAND_TIMEOUT")}
                         | {"llm": config.llm.model}
                         if expand_on(config.llm) else "off"),
    }
    config_fp = hashlib.sha1(
        json.dumps(config_snapshot, sort_keys=True).encode()).hexdigest()[:12]

    REPORTS_DIR.mkdir(exist_ok=True)
    outcomes_path = REPORTS_DIR / f"{name}.outcomes.jsonl"
    query_ids = {q["id"] for q in queries}
    done = {qid: o for qid, o in _load_done(outcomes_path, config_fp).items() if qid in query_ids}
    if require_resume and not done:
        # 补跑模式:配置指纹一条都对不上说明环境变了,宁可退出也不整轮重算
        raise SystemExit(f"[resume] {outcomes_path.name} 没有配置指纹 {config_fp} 的已完成行,放弃补跑")
    if done:
        print(f"[resume] {outcomes_path.name} 已有 {len(done)} 条完成(fp={config_fp}),跳过重算")

    outcomes: list[QueryOutcome] = []
    for i, q in enumerate(queries):
        if q["id"] in done:
            outcome = done[q["id"]]
            outcomes.append(outcome)
            print(f"[{i+1}/{len(queries)}] {q['id']} "
                  f"{'ERR' if outcome.error else len(outcome.injected)} (resumed)")
    todo = [(i, q) for i, q in enumerate(queries) if q["id"] not in done]
    # 查询彼此独立(静态评测不做反馈),远端 API 评测台可以并发;本地 CPU 模型服务请保持 1
    sem = asyncio.Semaphore(max(1, concurrency))

    class _Abort(Exception):
        pass

    with outcomes_path.open("a", encoding="utf-8") as jf:
        async def one(i: int, q: dict) -> None:
            async with sem:
                outcome = await _evaluate_query(engine, q)
            outcomes.append(outcome)
            record = dataclasses.asdict(outcome)
            record["config_fp"] = config_fp
            jf.write(json.dumps(record, ensure_ascii=False) + "\n")
            jf.flush()
            errs = sum(1 for o in outcomes if o.error)
            if len(outcomes) >= 20 and errs / len(outcomes) > ERROR_ABORT_RATE:
                raise _Abort(f"错误率 {errs}/{len(outcomes)} 超阈值,中止本轮")
            print(f"[{i+1}/{len(queries)}] {q['id']} "
                  f"{'ERR' if outcome.error else len(outcome.injected)}", flush=True)

        try:
            await asyncio.gather(*(one(i, q) for i, q in todo))
        except _Abort as e:
            raise SystemExit(str(e))
    order = {q["id"]: i for i, q in enumerate(queries)}
    outcomes.sort(key=lambda o: order[o.query_id])  # 并发完成顺序不定,报表按查询顺序

    report = evaluate(outcomes)
    payload = {
        "name": name,
        "config": config_snapshot,
        "config_fp": config_fp,
        "report": report,
        "outcomes": [dataclasses.asdict(o) for o in outcomes],
    }
    (REPORTS_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORTS_DIR / f"{name}.md").write_text(_to_md(payload), encoding="utf-8")
    print(_to_md(payload))
    await engine.drain(timeout=60)
    await pg.close()


def _fmt(v: float) -> str:
    return f"{v:.1%}"


def _to_md(payload: dict) -> str:
    r = payload["report"]
    lines = [
        f"# 召回评测报表: {payload['name']}", "",
        "| 指标 | 值 | 达标线 |", "|---|---|---|",
        f"| Recall@6 | {_fmt(r['recall_at_k'])} | ≥90% |",
        f"| Precision | {_fmt(r['precision'])} | ≥80% |",
        f"| 静默率 | {_fmt(r['silence_rate'])} | ≥90% |",
        f"| MRR | {r['mrr']:.3f} | 报告 |",
        "", f"查询数: {r['counts']}", "",
    ]
    if r.get("latency_ms"):
        lines += [f"match 延迟(ms): {r['latency_ms']}", ""]
    if r.get("jev_status"):
        lines += [f"Jev 状态: {r['jev_status']}", ""]
    if r.get("llm_judge_status"):
        lines += [f"LLM 判官状态: {r['llm_judge_status']}", ""]
    if r.get("expansion_status"):
        lines += [f"查询扩写状态: {r['expansion_status']}", ""]
    if r.get("rerank_failed"):
        lines += [f"精排失败(已降级,续跑会重做): {r['rerank_failed']}", ""]
    if r.get("jev_wait_ms"):
        lines += [f"Jev 额外等待(ms): {r['jev_wait_ms']}", ""]
    lines += ["## Miss 归因", ""]
    for stage, n in sorted(r["attribution_histogram"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {stage}: {n}")
    lines += ["", "## 分领域", "", "| 领域 | Recall@6 | Precision | MRR |", "|---|---|---|---|"]
    for d, dr in r["by_domain"].items():
        lines.append(f"| {d} | {_fmt(dr['recall_at_k'])} | "
                     f"{_fmt(dr['precision'])} | {dr['mrr']:.3f} |")
    lines += ["", f"旋钮快照: `{json.dumps(payload['config'], ensure_ascii=False)}`", ""]
    return "\n".join(lines)


def compare(a: str, b: str) -> None:
    pa = json.loads((REPORTS_DIR / f"{a}.json").read_text(encoding="utf-8"))
    pb = json.loads((REPORTS_DIR / f"{b}.json").read_text(encoding="utf-8"))
    print(f"| 指标 | {a} | {b} | Δ |")
    print("|---|---|---|---|")
    for key in ("recall_at_k", "precision", "silence_rate", "mrr"):
        va, vb = pa["report"][key], pb["report"][key]
        print(f"| {key} | {va:.3f} | {vb:.3f} | {vb - va:+.3f} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="run")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--require-resume", action="store_true", help="只补跑:没有可续的已完成行就退出")
    ap.add_argument("--concurrency", type=int, default=1, help="并发查询数(远端 API 评测台用;本地 CPU 模型保持 1)")
    args = ap.parse_args()
    if args.compare:
        compare(*args.compare)
    else:
        asyncio.run(run(args.name, args.limit, args.require_resume, args.concurrency))


if __name__ == "__main__":
    main()
