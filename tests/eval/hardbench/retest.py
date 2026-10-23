"""成熟记忆库重做:base 与记忆组都做错的题,换成记忆组跑完全程后的最终记忆库再做一遍,
同时闭卷再做一遍作对照——看"学够了"之后能不能把以前没做出来的题救回来(RSI 的最低要求)。

不学习、不反馈,只读实验库的一份副本。两段:
  prime  对每道题用最终库 prime(默认两段判定、判官超时放宽),注入集合冻结落盘 primes.jsonl;
         同时记一次现行单请求判定的注入,便于对照。
  solve  只对有注入的题作答两次:mem(带冻结的注入)与 ctl(闭卷),落盘 solves.jsonl,可断点续跑。
防泄漏:剔除从这道题本身、以及与它近重复(词 3-gram Jaccard ≥0.5)的任何题学到/改过的记忆。

  .venv-eval/bin/python -m tests.eval.hardbench.retest --lib self_v3 --stage prime
  .venv-eval/bin/python -m tests.eval.hardbench.retest --lib self_v3 --stage solve
  .venv-eval/bin/python -m tests.eval.hardbench.retest --lib self_v3 --report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import os
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from tests.eval.hardbench.rsi import HOOK_MAX, HOOK_MIN_CONF, OUT, ROOT, _grams
from tests.eval.hardbench.run import DATA, JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.hardbench.grading import failure_kind, judge_grade, outcome
from tests.eval.opencode_runner import CLOSED_BOOK
from tests.eval.realbench.uplift_eval import api_key, ask_with_retry, mcnemar_p, render_block

TAG = "phys"
COPY_DB = "ce_rsi_retest"


def _load(tag: str, arm: str) -> dict[str, dict]:
    out = {}
    for line in (OUT / f"{tag}.{arm}.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["id"]] = r
    return out


def _ok(rows: dict, pid: str) -> bool | None:
    return outcome(rows.get(pid))


def targets(lib: str) -> tuple[list[str], dict[str, set[str]], dict]:
    """(base 与 lib 组都做错的题, 每题要剔除的记忆 id, 题目表)。"""
    meta = json.loads((OUT / f"{TAG}.meta.json").read_text(encoding="utf-8"))
    order = meta["order"]
    problems = {p["id"]: p for p in json.loads((DATA / "problems.json").read_text(encoding="utf-8"))}
    base, arm = _load(TAG, "base"), _load(TAG, lib)
    touched = defaultdict(set)
    for r in arm.values():
        for x in r.get("lessons") or []:
            touched[r["id"]].update(x.get("ids") or [])
    grams = {i: _grams(problems[i]["problem"]) for i in order}
    pids = [i for i in order if _ok(base, i) is False and _ok(arm, i) is False]
    excl = {}
    for i in pids:
        g = grams[i]
        near = {j for j in order if j == i or (g and grams[j] and len(g & grams[j]) / len(g | grams[j]) >= 0.5)}
        excl[i] = set().union(*(touched[j] for j in near))
    return pids, excl, problems


async def stage_prime(args) -> None:
    import asyncpg
    from dotenv import load_dotenv

    import app.matching.pipeline as pipeline
    from app import model_registry, settings
    from app.config import Config
    from app.core.engine import Engine
    from app.llm_client import set_default_configs
    from app.storage.pg_store import PGStore
    from app.storage.qdrant_store import QdrantStore

    pids, excl, problems = targets(args.lib)
    load_dotenv(ROOT / ".env")
    base = Config.from_env()
    src = PGStore(replace(base.pg, database=args.source_db))
    await src.connect()
    try:
        reg = await model_registry.fetch(src)
        values = await settings.read(src, [k for k in settings.SPECS if k.startswith("model.")])
    finally:
        await src.close()
    config = model_registry.apply_roles(base, reg, values)
    conn = await asyncpg.connect(dsn=base.pg.dsn, timeout=10)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{COPY_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{COPY_DB}" TEMPLATE "ce_rsi_{TAG}_{args.lib}"')
    finally:
        await conn.close()
    config = replace(config, pg=replace(config.pg, database=COPY_DB),
                     qdrant=replace(config.qdrant, collection_name=f"rsi_{TAG}_{args.lib}"))
    set_default_configs(config.llm, config.embedding)
    pg = PGStore(config.pg)
    await pg.connect()
    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    pipeline._EXPLORATION_RATE = 0.0

    mode = {"two_stage": False}
    real_get = settings.get
    settings.get = lambda k: (mode["two_stage"] if k == "judge.two_stage"
                              else args.judge_timeout if k == "judge.timeout" else real_get(k))
    cur: dict = {}

    def keep(enzymes):
        return [e for e in (enzymes or []) if e.id not in cur["excl"]]
    real_single, real_two, real_fit = pipeline.judge_applicability, pipeline.judge_two_stage, pipeline.fit_pool

    async def single(context, enzymes, config, timeout=10.0, analogies=None):
        return await real_single(context, keep(enzymes), config, timeout=timeout, analogies=keep(analogies))

    async def two(context, enzymes, config, timeout=10.0, analogies=None):
        return await real_two(context, keep(enzymes), config, timeout=timeout, analogies=keep(analogies))
    pipeline.judge_applicability, pipeline.judge_two_stage = single, two
    pipeline.fit_pool = lambda context, enzymes: real_fit(context, keep(enzymes))

    engine = Engine(config, pg, qdrant)
    out_dir = OUT / f"retest_{args.lib}"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "primes.jsonl"
    done = {json.loads(l)["id"] for l in path.read_text(encoding="utf-8").splitlines() if l.strip()} \
        if path.exists() else set()
    print(f"目标题 {len(pids)}(base 与 {args.lib} 都错)· 已 prime {len(done)}", flush=True)
    for pid in pids:
        if pid in done:
            continue
        cur["excl"] = excl[pid]
        rec = {"id": pid, "excluded": len(excl[pid]), "excluded_ids": sorted(excl[pid])}
        for m in ("single", "two_stage"):
            mode["two_stage"] = m == "two_stage"
            trace: dict = {}
            res = await engine.match(problems[pid]["problem"], max_results=HOOK_MAX, source="prime", trace=trace)
            picked = [r for r in res if (r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory)
                      and r.enzyme.id not in excl[pid]][:HOOK_MAX]
            rec[m] = {"jev_status": trace.get("jev_status"), "judge": trace.get("judge"),
                      "trace": {k: trace[k] for k in ("recalled_ids", "refined_ids", "condition_killed_ids",
                          "rerank_raw", "reranked_ids", "rerank_failed", "scored", "dosed_ids", "jev",
                          "jev_none", "jev_status", "judge", "llm_judge_status", "llm_p") if k in trace},
                      "leak_dropped": sum(r.enzyme.id in excl[pid] for r in res),
                      "memories": [{"id": r.enzyme.id, "version": r.enzyme.version,
                                    "then": r.enzyme.then, "because": r.enzyme.because,
                                    "severity": r.enzyme.severity.value, "confidence": r.enzyme.confidence,
                                    "reason": r.reason, "p": r.applicability,
                                    "scope": "general" if "scope:general" in r.enzyme.tags else "specific"}
                                   for r in picked]}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  {pid:<26} 单请求 {rec['single']['jev_status']} 注入 {len(rec['single']['memories'])} · "
              f"两段 {rec['two_stage']['jev_status']}/{rec['two_stage']['judge']} 注入 "
              f"{len(rec['two_stage']['memories'])}", flush=True)
    await pg.close()


def run_dir(lib: str, name: str) -> Path:
    """New runs live together; keep old saved runs readable without migration."""
    base = OUT / f"retest_{lib}"
    current, legacy = base / "runs" / name, base / name
    return legacy if legacy.exists() and not current.exists() else current


def output_dir(args) -> Path:
    return Path(args.output_dir).resolve() if args.output_dir else run_dir(args.lib, args.run_name)


def load_primes(args) -> list[dict]:
    path = OUT / f"retest_{args.lib}" / "primes.jsonl"
    eligible, _, _ = targets(args.lib)
    allowed = set(eligible)
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l)["id"] in allowed]


def load_solves(path: Path) -> dict[tuple[str, str], dict]:
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["arm"], row["id"]] = row
    return rows


def ensure_manifest(args, primes: list[dict]) -> dict:
    directory = output_dir(args)
    directory.mkdir(parents=True, exist_ok=True)
    data = {"protocol": "retest-v2-ungraded-is-unknown", "lib": args.lib, "mode": args.mode,
            "solver_protocol": "closed-book-v3-wallclock-and-tool-failure",
            "grading_protocol": "full-final-answer-requested-parts-v1",
            "judge_prompt_sha256": hashlib.sha256(JUDGE_PROMPT.encode()).hexdigest(),
            "runner": args.runner, "model": args.model, "judge_model": args.judge_model or args.model,
            "provider_name": getattr(args, "provider_name", ""),
            "thinking": args.thinking, "max_output_tokens": args.max_output_tokens,
            "frozen_primes_sha256": hashlib.sha256(json.dumps(primes, sort_keys=True).encode()).hexdigest(),
            "problems_sha256": hashlib.sha256((DATA / "problems.json").read_bytes()).hexdigest(),
            "eligible_ids": [r["id"] for r in primes],
            "scored_ids": [r["id"] for r in primes if r[args.mode]["memories"]]}
    if getattr(args, "answers_from", ""):
        source = Path(args.answers_from).resolve()
        data["answer_source"] = {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        data["answer_mode"] = "regrade_saved_only"
    data.update(solver_timeout_s=args.timeout, grading_timeout_s=args.grading_timeout,
                judge_max_output_tokens=4096, judge_thinking="off",
                max_output_tokens_scope="per_provider_request", generation_timeout_is_failure=True,
                closed_book_sha256=hashlib.sha256(CLOSED_BOOK.encode()).hexdigest())
    path = directory / "manifest.json"
    if path.exists():
        if json.loads(path.read_text()) != data:
            raise SystemExit("运行身份或冻结输入不同,请换 --run-name;禁止混合模型/模式/题集续跑")
    elif (directory / "solves.jsonl").exists():
        raise SystemExit("已有结果缺少 manifest,只可 --report;新模型请换 --run-name")
    else:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


async def stage_solve(args, *, grade_only: bool = False) -> None:
    if getattr(args, "answers_from", "") and not grade_only:
        raise SystemExit("--answers-from 只可用于 --stage grade,不会重新生成历史答案")
    out_dir = output_dir(args)
    primes = load_primes(args)
    ensure_manifest(args, primes)
    problems = {p["id"]: p for p in json.loads((DATA / "problems.json").read_text(encoding="utf-8"))}
    path, log_path = out_dir / "solves.jsonl", out_dir / "solves.log"
    if getattr(args, "answers_from", "") and not path.exists():
        source = load_solves(Path(args.answers_from).resolve())
        selected = {r["id"] for r in primes if r[args.mode]["memories"]}
        injections = {r["id"]: [m["id"] for m in r[args.mode]["memories"]] for r in primes}
        with path.open("x", encoding="utf-8") as f:
            for (arm, pid), previous in source.items():
                if pid not in selected or previous.get("error") or not previous.get("text"):
                    continue
                if previous.get("mode", args.mode) != args.mode or previous.get("injected", []) != (injections[pid] if arm == "mem" else []):
                    raise SystemExit(f"历史答案 {arm}/{pid} 的注入不匹配当前冻结输入,请使用对应模式/原始 primes")
                row = {**previous, "previous_grade": {k: previous.get(k) for k in
                    ("correct", "judge_ok", "graded_by", "pred")},
                    "correct": None, "judge_ok": False, "grade_status": "ungraded"}
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    saved = load_solves(path)

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [retest] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    env = dict(os.environ, PI_OFFLINE="1", PI_TELEMETRY="0")
    if args.runner == "pi" and args.key_from:
        env[args.key_env] = api_key(args.key_from)
    cwd = out_dir / "closed_book"
    cwd.mkdir(parents=True, exist_ok=True)
    solver = SimpleNamespace(model=args.model, thinking=args.thinking, timeout=args.timeout, max_wait=args.max_wait,
                             purpose="solve",
                             runner=args.runner, opencode_bin=args.opencode_bin, max_output_tokens=args.max_output_tokens,
                             provider_name=args.provider_name, provider_config=args.provider_config)
    judge = SimpleNamespace(**{**vars(solver), "model": args.judge_model or args.model,
                              "thinking": "off", "timeout": args.grading_timeout, "max_output_tokens": 4096,
                              "purpose": "grade", "service_retries": 0})
    sem, lock = asyncio.Semaphore(args.concurrency), asyncio.Lock()

    async def write(row: dict) -> None:
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def one(arm: str, rec: dict) -> None:
        async with sem:
            p = problems[rec["id"]]
            previous = saved.get((arm, rec["id"]))
            if previous and not previous.get("error"):
                row = dict(previous)             # 补判分绝不重新作答
            else:
                mems = rec[args.mode]["memories"] if arm == "mem" else []
                prompt = (f"{render_block(mems)}\n\n" if mems else "") + solve_prompt(p)
                res = await ask_with_retry(prompt, solver, env, cwd, lambda msg: log(f"{arm}/{rec['id']} {msg}"))
                row = {"id": rec["id"], "arm": arm, "mode": args.mode, "model": args.model,
                       "runner": args.runner, "injected": [m["id"] for m in mems],
                       **{k: res.get(k) for k in ("secs", "retries", "usage", "stop", "thinking_chars", "served_model", "session_id", "cost", "protocol_error", "tool_attempts", "budget_exhausted", "budget_error")},
                       "text": res.get("text") or ""}
                if not res["ok"]:
                    row["error"] = res.get("error", "unknown")
                    await write(row)
                    log(f"  {arm} {rec['id']} 作答错误 {row['error'][:180]}")
                    return
            g = grade_local(p, row["text"])
            if g.pop("need_judge", False):
                row.update(g, correct=None, judge_ok=False, graded_by="judge", grade_status="ungraded")
                await write(row)                 # 先存答案,判官超时/中断也不丢失
                for _ in range(2):
                    jr = await ask_with_retry(JUDGE_PROMPT.format(domain=p["domain"], problem=p["problem"],
                        answer=p["answer"], pred=g["pred"]), judge, env, cwd, lambda msg: log(f"grade/{arm}/{rec['id']} {msg}"))
                    verdict = parse_verdict(jr.get("text") or "") if jr["ok"] else None
                    row.update(judge_grade(verdict, jr, model=judge.model))
                    if verdict is not None:
                        break
            else:
                row.update(g, grade_status="graded", judge_ok=True, grading_error=None)
            await write(row)
            value = outcome(row)
            mark = "未判定" if value is None else "对" if value else "错"
            log(f"  {arm} {rec['id']:<26} {mark} 注入 {len(row['injected'])} {row.get('secs')}s")

    todo = [(arm, rec) for rec in primes if rec[args.mode]["memories"] for arm in ("mem", "ctl")
            if outcome(saved.get((arm, rec["id"]))) is None
            and (not grade_only or ((saved.get((arm, rec["id"])) or {}).get("text")
                 and not (saved.get((arm, rec["id"])) or {}).get("error")))]
    if args.limit:
        allowed = {r["id"] for r in primes if r[args.mode]["memories"]}
        allowed = set(sorted(allowed)[:args.limit])
        todo = [(arm, rec) for arm, rec in todo if rec["id"] in allowed]
    log(f"{args.runner}/{args.model} · 有注入 {sum(bool(r[args.mode]['memories']) for r in primes)}/{len(primes)} · 待处理 {len(todo)} 臂")
    await asyncio.gather(*(one(arm, rec) for arm, rec in todo))
    report(args)


def report(args) -> str:
    out_dir = output_dir(args)
    primes = load_primes(args)
    rows = load_solves(out_dir / "solves.jsonl")
    lines = [f"# 成熟记忆库重测: {args.lib} / {out_dir.name}", "",
             "判分失败不算错;只比较两臂均有有效判分的同题。旧实验判分失败的题不列入错题池。", "",
             "本实验仅覆盖原 GLM 基线与记忆组都答错、且冻结选择有注入的题,不能外推到全题集。", ""]
    for m in ("single", "two_stage"):
        st = Counter(f"{r[m]['jev_status']}/{r[m]['judge'] or '-'}" for r in primes)
        n = [len(r[m]["memories"]) for r in primes]
        lines.append(f"- 冻结选择 {m}:有注入 {sum(x > 0 for x in n)}/{len(primes)},平均 {sum(n) / max(1, len(n)):.2f} 条;判官 {dict(st)}")
    selected = [r["id"] for r in primes if r[args.mode]["memories"]]
    lines += ["", "| 组 | 应完成 | 有效判分 | 未判定 | 作答服务错误 | 尚无记录 | 其中工具违规 |", "|---|---|---|---|---|---|---|"]
    for arm in ("ctl", "mem"):
        counts = Counter(failure_kind(rows.get((arm, pid))) for pid in selected)
        violations = sum(bool((rows.get((arm, pid)) or {}).get('protocol_error')) for pid in selected)
        lines.append(f"| {arm} | {len(selected)} | {counts['graded']} | {counts['ungraded']} | {counts['solver_error']} | {counts['missing']} | {violations} |")
    ids = [pid for pid in selected if all(outcome(rows.get((arm, pid))) is not None for arm in ("mem", "ctl"))]
    if ids:
        mem, ctl = (sum(outcome(rows[arm, i]) for i in ids) for arm in ("mem", "ctl"))
        b = sum(outcome(rows['mem', i]) and not outcome(rows['ctl', i]) for i in ids)
        c = sum(outcome(rows['ctl', i]) and not outcome(rows['mem', i]) for i in ids)
        lines += ["", f"有效配对 {len(ids)}/{len(selected)}。无记忆 {ctl}/{len(ids)} ({ctl/len(ids):.1%});有记忆 {mem}/{len(ids)} ({mem/len(ids):.1%})。",
                  f"仅记忆答对 {b},仅闭卷答对 {c};净变化 {b-c:+d};McNemar p={mcnemar_p(b,c):.4g}。"]
    if (out_dir / 'manifest.json').exists():
        meta = json.loads((out_dir / 'manifest.json').read_text())
        if meta.get('answer_source'):
            supplied = {i for a, i in rows if (a, i) in rows and all((b, i) in rows for b in ('mem', 'ctl'))}
            lines += ["", "本次仅对历史已保存答案重新判分,没有重新作答。原始文件未逐行记录模型的,不能把本次判官当作原作答模型。",
                      f"源文件在合格题中保存过两臂的有 {len(supplied)} 题;其余没有历史答案可补判。",
                      f"原答案: {meta['answer_source']['path']};判分: {meta['runner']} / {meta['judge_model']}。"]
        else:
            lines += ["", f"作答: {meta['runner']} / {meta['model']};判分: {meta['judge_model']};冻结模式: {meta['mode']}。"]
    text = '\n'.join(lines) + '\n'
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'report.md').write_text(text)
    print(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default="self_v3")
    ap.add_argument("--stage", choices=("prime", "solve", "grade"))
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--mode", default="two_stage", choices=("single", "two_stage"), help="solve 用哪种判定的注入")
    ap.add_argument("--judge-timeout", type=float, default=15.0)
    ap.add_argument("--grading-timeout", type=float, default=180.0)
    ap.add_argument("--source-db", default="cognitive_enzyme_test")
    ap.add_argument("--runner", choices=("pi", "opencode", "openai"), default="opencode")
    ap.add_argument("--model", default="opencode/deepseek-v4.1-flash-free")
    ap.add_argument("--provider-name", default="magpie")
    ap.add_argument("--provider-config", default="~/.pi/agent/models.json")
    ap.add_argument("--judge-model", default="")
    ap.add_argument("--opencode-bin", default=os.environ.get("CE_EVAL_OPENCODE_BIN", ""))
    ap.add_argument("--max-output-tokens", type=int, default=32000)
    ap.add_argument("--run-name", default="deepseek_flash_free")
    ap.add_argument("--output-dir", default="")
    ap.add_argument("--answers-from", default="", help="grade 专用:在新运行目录重判指定 solves.jsonl,保留原文件及原判分")
    ap.add_argument("--limit", type=int, default=0, help="只处理固定排序的前 N 个有注入案例;0=全部")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--key-from", default="cc-switch-zhipu-glm")
    ap.add_argument("--key-env", default="ZAI_CODING_CN_API_KEY")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--max-wait", type=float, default=120.0)
    args = ap.parse_args()
    if args.report:
        report(args)
    elif args.stage == "prime":
        asyncio.run(stage_prime(args))
    elif args.stage == "solve":
        asyncio.run(stage_solve(args))
    elif args.stage == "grade":
        asyncio.run(stage_solve(args, grade_only=True))
    else:
        raise SystemExit("--stage prime|solve 或 --report")


if __name__ == "__main__":
    main()
