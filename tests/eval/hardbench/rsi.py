"""hardbench 经验流(RSI):同一批难题按固定顺序做两遍——不带记忆(base)和带认知酶记忆(self / teacher)——
看系统从前面做过的题里积累的经验,能不能让后面的题做得更好。

arms:
  base     闭卷,不注入(可以全并行)
  base2    闭卷再做一遍,和 base 完全一样:两次闭卷之间的翻转量就是模型自身随机性(噪声底线),
           记忆组相对 base 的翻转要明显超过它才算记忆真起了作用
  self     只从自己做对、且最终答案经核对正确的题里提炼经验:没有外部老师,奖励可验证(RLVR 式)
  teacher  在 self 的基础上,做错的题也学:给出标准答案(有官方解答的连解答一起),提炼"错在哪、下次怎么避免"
  self_hard / teacher_hard  学习规则同 self / teacher,但只学"模型闭卷做不出来"的题:base 组已经做对的题,
           说明这类知识模型自己就有(预训练已覆盖),不提炼、不入库;base 里没有这题的结果时照常学(不确定就不拦)
  self_v2  规则与 self 完全一样,从零重跑:和 self_hard 用同一判官、同一并发,两者唯一差别就是过滤
           (self 前半程用的是旧判官、并发 3~5,不能直接和 self_hard 比)
  self_v3 / teacher_v3  规则同 self / teacher,从零重跑:判官 token 预算修复后(self_v2、teacher
           那两轮判官大面积 422 失灵、退回盲注 5 条),用来回答"判官正常工作时记忆还有没有效"
  reflect  只从失败里学,且只存验证过的经验:做对的题不学(模型已会);做错的题只告诉它"错了、正确最终答案是 X"
           (不给官方解答),让它复盘找出第一处错误并提炼可迁移的教训 → 防泄漏过滤 → 验证门:把这些教训注入后
           不看答案重做本题,做对了才入库。self 只学自己已会的、没有新信息;teacher 依赖官方解答;reflect 的新信息
           只来自"对/错 + 正确答案"这种真实 agent 场景里也拿得到的信号(测试失败、报错、用户纠正)。
           复盘、作答、验证重做都走 pi + --model;提炼仍用库里绑定的引擎 LLM(--learn-model 可换)。

记忆组每道题:
  1. prime:engine.match(题面),按 prime hook 的规则取注入(置信度 ≥0.25 或探索项,至多 6 条),
     渲染与 hooks/ce_prime_hook.py 一字不差,放在题目前面
  2. pi + GLM-5.3 作答(思考 high,不给工具),判分同 hardbench.run
  3. 反馈:按产品协议把核对结果 report 给注入过的记忆(report_batch:第一条全额,其余按名次衰减)
  4. 学习:把"题目 + 解答 + 核对结果"当作一段会话交给产品的 capture 提炼(领域方法与通用方法论都要,后者标
     scope:general)→ 防泄漏过滤 → 撞库:没撞上 engine.harvest(质量门、领域归类),撞上了由 capture 裁决
     新增 / 补充 / 强化——同一条方法论在很多题上被反复印证,会积累经验样本和置信度 → 碰过的记忆把这道题记为经验样本

防过拟合:
  - 开跑前定死:认知酶全用默认参数(只关探索槽,和 realbench 经验流一致);提示词通用,不针对这批题调;
  - 经验必须通用:提示词禁止写本题的数字、对象和答案;程序再过滤——含本题标准答案、含题面里 3 位以上的数、
    或连续 8 个词照抄题面的一律丢弃,丢弃数进报表;
  - 不挑题:集合按固定种子打乱后整体做,不按模型会不会筛;
  - 配对:base 与记忆组做同一批题,逐题比较(McNemar),另报后半程与去掉近重复题后的结果;
  - 因果顺序:并发 B 道一批,一批做完才学习,第 i 题的经验最早用于第 i+B 题。

模型:检索、精排、判官、引擎 LLM 用测试平台库(--source-db)里绑定的模型(只读它的模型注册表与绑定);
记忆写进各自独立的库 ce_rsi_<tag>_<arm> 和向量集合 rsi_<tag>_<arm>,互不干扰。

  .venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --sets physelite --arm base
  .venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --sets physelite --arm self
  .venv-eval/bin/python -m tests.eval.hardbench.rsi --tag phys --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from tests.eval.hardbench.run import DATA, JUDGE_PROMPT, grade_local, parse_verdict, solve_prompt
from tests.eval.hardbench.grading import failure_kind, judge_grade, outcome
from tests.eval.realbench.uplift_eval import SLOTS, api_key, ask_with_retry, mcnemar_p, render_block

ROOT = Path(__file__).resolve().parents[3]
OUT = DATA / "rsi"
HOOK_MIN_CONF, HOOK_MAX = 0.25, 6
ARMS = ("base", "base2", "self", "self_v2", "teacher", "seed", "seed_self", "self_hard", "teacher_hard",
        "self_v3", "teacher_v3", "reflect")

REFLECT_PROMPT = """你在复盘一道自己做错的题。下面是题目、你当时的解答,以及核对结果(只告诉你正确的最终答案,没有标准解答)。

# 题目
{problem}

# 你当时的解答(末尾部分)
{attempt}

# 核对结果
你的最终答案是错的。正确的最终答案是:{answer}

请按顺序完成:
1. 错误定位:找出解答里第一个出错的地方(概念、建模、公式、近似、计算还是审题),说明错在哪里、为什么会错。
2. 改正:从出错处起重新推导,直到得到上面的正确答案;推不到就如实说明,不要硬凑。
3. 教训:写 1~2 条下次做别的题也用得上的经验——在什么情形下(触发条件)、应该怎么做、为什么。
   教训要能迁移到同类的其他题目,不要出现本题的具体数字、对象名称和答案。"""

MIN_SALIENCE = 0.55          # 与 capture_session 的默认值一致


def session_transcript(p: dict, row: dict, arm: str) -> str | None:
    """把一道题做成一段会话记录,交给产品的 capture 提炼。self 只学做对的;teacher 做错的也学(给标准答案与官方解答)。"""
    if outcome(row) is None:
        return None
    head = f"[user] {p['problem']}\n\n[assistant] {row['text'][-6000:]}\n\n"
    if row.get("correct"):
        return head + "[verification] 最终答案经核对正确。"
    if arm.removesuffix("_hard").startswith("teacher") and not row.get("error"):
        official = f"\n\n[official solution] {p['solution'][-4000:]}" if p.get("solution") else ""
        return head + f"[verification] 最终答案经核对是错的。正确的最终答案:{p['answer']}{official}"
    return None


def base_solved(base: dict[str, bool], pid: str) -> bool:
    """base 组(闭卷)是否已经做对这道题;没有结果时返回 False(不确定就不拦)。"""
    return base.get(pid, False)


def load_base(tag: str) -> dict[str, bool]:
    """base 组已完成(非 error)的题 → 是否做对。"""
    path, _ = paths(tag, "base")
    out: dict[str, bool] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                result = outcome(r)
                if result is not None:
                    out[r["id"]] = result
    return out


# ── 题目流 ─────────────────────────────────────────────────────

def load_stream(sets: list[str], limit: int, seed: int) -> list[dict]:
    pool = [p for p in json.loads((DATA / "problems.json").read_text(encoding="utf-8")) if p["set"] in sets]
    random.Random(seed).shuffle(pool)
    return pool[:limit] if limit else pool


def _grams(text: str) -> set[str]:
    w = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {" ".join(w[i:i + 3]) for i in range(len(w) - 2)}


def near_duplicates(stream: list[dict], threshold: float = 0.5) -> set[str]:
    """与流里前面某道题高度相似(词 3-gram Jaccard ≥ threshold)的题。"""
    seen: list[set[str]] = []
    dup = set()
    for p in stream:
        g = _grams(p["problem"])
        if g and any(len(g & s) / len(g | s) >= threshold for s in seen if s):
            dup.add(p["id"])
        seen.append(g)
    return dup


# ── 防泄漏 ─────────────────────────────────────────────────────

def leak_reason(text: str, p: dict) -> str | None:
    t = text or ""
    norm = lambda s: re.sub(r"\s+", " ", re.sub(r"[$\\{}]", "", s)).strip()     # noqa: E731
    ans = norm(str(p.get("answer") or ""))
    if len(ans.replace(" ", "")) >= 2 and p["kind"] != "choice" and re.search(
            rf"(?<!\w)(?<!\d\.){re.escape(ans)}(?!\w)(?!\.\d)", norm(t)):
        return "answer"
    if any(n in t for n in set(re.findall(r"\d{3,}", p["problem"]))):
        return "problem_number"
    pw = re.findall(r"\w+", p["problem"].lower())
    grams = {" ".join(pw[i:i + 8]) for i in range(len(pw) - 7)}
    lw = re.findall(r"\w+", t.lower())
    if any(" ".join(lw[i:i + 8]) in grams for i in range(len(lw) - 7)):
        return "copied"
    return None


# ── 学习:产品 capture 的流程 + 防泄漏 ─────────────────────────────

def _scope(cand: dict) -> str:
    return "general" if "scope:general" in cand["tags"] else "specific"


def screen(cands: list[dict], p: dict, stats: dict) -> tuple[list[dict], list[dict]]:
    """提炼出的候选过显著度门与防泄漏过滤:返回 (留下的候选, 被丢弃的记录)。"""
    from app.learning import capture

    kept, out = [], []
    for cand in cands[:capture._MAX_CANDIDATES]:
        stats["extracted"] += 1
        stats[f"scope_{_scope(cand)}"] += 1
        item = {"scope": _scope(cand), "text": cand["then"][:300]}
        if cand.get("salience", 0) < MIN_SALIENCE:
            stats["low_salience"] += 1
            out.append({**item, "decision": "low_salience"})
            continue
        reason = leak_reason(f"{cand['trigger']} {cand['then']} {cand['because']}", p)
        if reason:
            stats[f"leak_{reason}"] += 1
            out.append({**item, "decision": f"leak:{reason}"})
            continue
        kept.append(cand)
    return kept, out


async def store_lessons(engine, p: dict, transcript: str, tag: str, arm: str, stats: dict,
                        cands: list[dict] | None = None) -> list[dict]:
    """提炼候选 → 防泄漏过滤 → 撞库:没撞上就 harvest,撞上了由 capture 裁决新增 / 补充 / 强化 / 推翻 / 跳过。
    碰过的记忆都把这道题记为经验样本。cands 给定时(reflect:已提炼、已过滤、已验证)跳过提炼与过滤。"""
    from app.learning import capture

    summary = defaultdict(int)
    if cands is None:
        cands, out = screen(await capture.extract_candidates(transcript, domain=""), p, stats)
    else:
        out = []
    for cand in cands:
        item = {"scope": _scope(cand), "text": cand["then"][:300]}
        cand["tags"] = [*cand["tags"], f"rsi:{tag}"]
        touched: list[str] = []
        hits = await capture._find_similar(engine, cand)
        if not hits:
            res = await engine.harvest(insight=cand["then"], context=cand["trigger"], domain=cand.get("domain", ""),
                                       tags=cand["tags"])
            decision = f"add:{res.get('decision') or res.get('status')}"
            if res.get("id"):
                touched.append(res["id"])
        else:
            verdict = await capture.reconcile(cand, hits)
            if not verdict:
                decision = "reconcile_failed"
            elif leak_reason(f"{verdict.get('then', '')} {verdict.get('because', '')}", p):
                decision = "leak:merged"
            else:
                await capture._apply(engine, cand, verdict, summary, touched, session_id="")
                decision = (verdict.get("action") or "SKIP").lower()
        stats[decision] += 1
        for eid in dict.fromkeys(touched):
            await engine.learn_exemplar(eid, p["problem"], source=f"rsi_{arm}")
        out.append({**item, "decision": decision, "ids": list(dict.fromkeys(touched))})
    return out


# ── 引擎(独立的库与向量集合)────────────────────────────────────

async def make_engine(tag: str, arm: str, source_db: str, fresh: bool, seed_db: str = "", learn_model: str = ""):
    import asyncpg
    from dotenv import load_dotenv

    import app.matching.pipeline as pipeline
    from app import model_registry, settings
    from app.config import Config
    from app.core.engine import Engine
    from app.llm_client import set_default_configs
    from app.storage.pg_store import PGStore
    from app.storage.qdrant_store import QdrantStore

    load_dotenv(ROOT / ".env")
    base = Config.from_env()
    src = PGStore(replace(base.pg, database=source_db))
    await src.connect()
    try:
        reg = await model_registry.fetch(src)
        values = await settings.read(src, [k for k in settings.SPECS if k.startswith("model.")])
    finally:
        await src.close()
    config = model_registry.apply_roles(base, reg, values)
    config = replace(config, llm=replace(config.llm, timeout=max(config.llm.timeout or 0, 300.0)))  # 思考模型提炼偶尔较慢
    if learn_model:              # 提炼 / 撞库裁决 / 质量门 / 领域归类换成更快的模型(关思考);作答不受影响
        config = replace(config, llm=replace(config.llm, model=learn_model, extra={"thinking": {"type": "disabled"}}))
    db, coll = f"ce_rsi_{tag}_{arm}", f"rsi_{tag}_{arm}"
    config = replace(config, pg=replace(config.pg, database=db), qdrant=replace(config.qdrant, collection_name=coll))
    assert config.embedding.api_key and config.typesafe.available and config.llm.available, "模型绑定不全"

    conn = await asyncpg.connect(dsn=base.pg.dsn, timeout=10)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
        if exists and fresh:
            await conn.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
            exists = False
        if not exists:           # 带种子的组:从种子库复制一份起步(模板库此刻不能有连接)
            await conn.execute(f'CREATE DATABASE "{db}"' + (f' TEMPLATE "{seed_db}"' if seed_db else ""))
    finally:
        await conn.close()
    set_default_configs(config.llm, config.embedding)
    pg = PGStore(config.pg)
    await pg.connect()
    await pg.init_schema()
    qdrant = QdrantStore(config.qdrant, config.embedding)
    await qdrant.connect()
    if fresh:
        for name in (qdrant.collection, qdrant.exemplar_collection, qdrant.lexical_collection):
            if await qdrant._client.collection_exists(name):
                await qdrant._client.delete_collection(name)
    await qdrant.ensure_collection()
    if fresh and seed_db:                     # 种子记忆的向量、词面与经验样本索引按本组的集合重建
        from app.storage.sync import SyncManager
        await SyncManager(pg, qdrant).full_sync(concurrency=4)
    pipeline._EXPLORATION_RATE = 0.0
    return Engine(config, pg, qdrant), config


# ── 主流程 ─────────────────────────────────────────────────────

def paths(tag: str, arm: str) -> tuple[Path, Path]:
    """每组各写各的文件:几组同时跑时,长行追加不会互相穿插。"""
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / f"{tag}.{arm}.jsonl", OUT / f"{tag}.{arm}.log"


def load_rows(tag: str) -> dict[str, dict]:
    rows = {}
    for path in sorted(OUT.glob(f"{tag}.*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[f"{r['arm']}|{r['id']}"] = r
    return rows


async def run(args) -> None:
    sets = [s for s in args.sets.split(",") if s]
    stream = load_stream(sets, args.limit, args.seed)
    path, log_path = paths(args.tag, args.arm)
    meta_path = OUT / f"{args.tag}.meta.json"
    meta = {"sets": sets, "limit": args.limit, "seed": args.seed, "order": [p["id"] for p in stream]}
    if meta_path.exists():
        saved = json.loads(meta_path.read_text(encoding="utf-8"))["order"]
        new = meta["order"]
        if saved[:len(new)] != new and new[:len(saved)] != saved:
            raise SystemExit(f"{meta_path} 里的题目顺序和这次不同:同一个 tag 的各组必须做同一批题(可以只做前 N 道)")
        if len(new) > len(saved):                 # 各组可以只做同一顺序的前 N 道;报表按最长的顺序,配对取交集
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    else:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    done = {k for k, r in load_rows(args.tag).items() if not r.get("error")}
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(OUT / f"{args.tag}.{args.arm}.app.log", encoding="utf-8")])

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{args.arm}] {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    env = dict(os.environ, PI_OFFLINE="1", PI_TELEMETRY="0")
    if args.key_from:
        env[args.key_env] = api_key(args.key_from)
    cwd = OUT / "pi_cwd"
    cwd.mkdir(exist_ok=True)
    solver = SimpleNamespace(model=args.model, thinking=args.thinking, timeout=args.timeout, max_wait=args.max_wait)
    judge = SimpleNamespace(model=args.model, thinking="low", timeout=900.0, max_wait=args.max_wait)
    sem, lock = asyncio.Semaphore(args.concurrency), asyncio.Lock()
    todo = [(i, p) for i, p in enumerate(stream) if f"{args.arm}|{p['id']}" not in done]
    if args.only == "base_wrong":     # 只做 base 闭卷做错的题(题序不变):看能不能把"模型不会"的题越学越多地救回来
        base_ok = load_base(args.tag)
        todo = [(i, p) for i, p in todo if base_ok.get(p["id"]) is False]
    log(f"tag={args.tag} sets={sets} 题目 {len(stream)} 待做 {len(todo)}{'(只做 base 错题)' if args.only else ''} "
        f"model={args.model} thinking={args.thinking}")

    async def solve(p: dict, memories: list[dict]) -> dict:
        prompt = (f"{render_block(memories)}\n\n" if memories else "") + solve_prompt(p)
        # 作答与判等价占同一个并发名额连续做完:分两次排队的话,判等价会排到几百道待解的题后面,
        # 全部解完之前一行结果都写不出来(base 组一次排进整条流)
        async with sem:
            res = await ask_with_retry(prompt, solver, env, cwd, log)
            row = {"secs": res.get("secs"), "retries": res.get("retries", 0), "usage": res.get("usage"),
                   "stop": res.get("stop"), "text": (res.get("text") or "")[-6000:]}
            if not res["ok"]:
                return {**row, "error": res.get("error", "unknown")}
            g = grade_local(p, res.get("text") or "")
            if g.pop("need_judge", False):
                jr = await ask_with_retry(JUDGE_PROMPT.format(domain=p["domain"], problem=p["problem"],
                                                              answer=p["answer"], pred=g["pred"]), judge, env, cwd, log)
                verdict = parse_verdict(jr.get("text") or "") if jr["ok"] else None
                g.update(judge_grade(verdict, jr, model=judge.model))
        return {**row, **g}

    async def write(row: dict) -> None:
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.arm in ("base", "base2"):
        async def one(i: int, p: dict) -> None:
            row = {"id": p["id"], "set": p["set"], "arm": args.arm, "pos": i, "injected": [], **(await solve(p, []))}
            await write(row)
            log(f"  [{i}] {p['set']:<11} {p['id']:<26} {'错误' if row.get('error') else ('对' if row.get('correct') else '错')} "
                f"{row.get('secs')}s")
        await asyncio.gather(*(one(i, p) for i, p in todo))
        report(args.tag)
        return

    from app.learning import capture

    started = any(k.startswith(f"{args.arm}|") for k in done)     # 续跑:保留这一组已经学到的记忆
    engine, config = await make_engine(args.tag, args.arm, args.source_db, fresh=not started,
                                       seed_db=args.seed_db if args.arm.startswith("seed") else "",
                                       learn_model=args.learn_model)
    log(f"引擎:库 {config.pg.database} · 集合 {config.qdrant.collection_name} · 向量 {config.embedding.model} · "
        f"精排 {config.embedding.rerank_model} · 判官 {config.typesafe.model} · LLM {config.llm.model}")
    from app.matching.selection import thresholds
    log(f"选择阈值 {thresholds()}")
    stats = defaultdict(int)

    hard_only = args.arm.endswith("_hard")
    base = load_base(args.tag) if hard_only else {}

    async def reflect_prepare(p: dict, row: dict) -> dict | None:
        """reflect:做错的题 → 复盘 → 提炼、过滤 → 把留下的教训注入后不看答案重做本题(验证门)。
        一批里各题并发做(复盘与验证重做都慢);入库仍在后面按题序逐条做。"""
        if outcome(row) is not False:
            return None
        prompt = REFLECT_PROMPT.format(problem=p["problem"], attempt=row["text"][-6000:], answer=p["answer"])
        # 复盘走和作答同一条 pi 通道(同模型、同思考档,自带限流退避、自己占全局槽位):实测直接调接口时
        # GLM-5.3 / 5.3-flash 在这类题上都关不掉思考,推理上万 token 后输出被截成空
        t0 = time.perf_counter()
        log(f"    复盘 {p['id']} 开始")
        async with sem:
            res = await ask_with_retry(prompt, solver, env, cwd, log)
        reflection = (res.get("text") or "").strip() if res["ok"] else ""
        log(f"    复盘 {p['id']} {'完成' if reflection else '失败:' + str(res.get('error', ''))[:120]} "
            f"{time.perf_counter() - t0:.0f}s · {len(reflection)} 字")
        if not reflection:
            return {"failed": "reflect"}
        transcript = (f"[user] {p['problem']}\n\n[assistant] {row['text'][-6000:]}\n\n"
                      f"[verification] 最终答案经核对是错的。正确的最终答案:{p['answer']}\n\n[reflection] {reflection}")
        t1 = time.perf_counter()
        async with SLOTS.hold():
            kept, dropped = screen(await capture.extract_candidates(transcript, domain=""), p, stats)
        log(f"    提炼 {p['id']} {time.perf_counter() - t1:.0f}s · 留下 {len(kept)} 条 · 丢弃 "
            f"{[x['decision'] for x in dropped]}")
        info = {"reflection": reflection[-4000:], "kept": kept, "dropped": dropped}
        if kept:
            mems = [{"id": f"cand{k}", "then": c["then"], "because": c.get("because", ""), "severity": "info",
                     "confidence": 0.5, "scope": _scope(c)} for k, c in enumerate(kept)]
            v = await solve(p, mems)
            info.update(verify_correct=bool(v.get("correct")) and not v.get("error"),
                        verify_secs=v.get("secs"), verify_error=v.get("error"))
            log(f"    验证 {p['id']} {'对' if info['verify_correct'] else '错'} {v.get('secs')}s")
        return info

    prepared: dict[str, dict | None] = {}

    async def learn(p: dict, row: dict) -> list[dict]:
        if args.arm == "reflect":
            if outcome(row) is None:
                return []
            if row.get("correct"):
                stats["skip_correct"] += 1
                return [{"decision": "skip_correct"}]
            info = prepared.get(p["id"]) or {"failed": "missing"}
            if info.get("failed"):
                stats[f"{info['failed']}_failed"] += 1
                return [{"decision": f"{info['failed']}_failed"}]
            row["reflect"] = {"reflection": info["reflection"], "kept": len(info["kept"]),
                              **{k: info.get(k) for k in ("verify_correct", "verify_secs", "verify_error")}}
            if not info["kept"]:
                return info["dropped"] or [{"decision": "no_candidates"}]
            if not info.get("verify_correct"):
                stats["unverified"] += len(info["kept"])
                return info["dropped"] + [{"scope": _scope(c), "text": c["then"][:300], "decision": "unverified"}
                                          for c in info["kept"]]
            stats["verified"] += len(info["kept"])
            async with SLOTS.hold():
                return info["dropped"] + await store_lessons(engine, p, "", args.tag, args.arm, stats,
                                                             cands=info["kept"])
        if hard_only:
            if p["id"] not in base:
                base.update(load_base(args.tag))          # base 组还在跑:重读一次
            if base_solved(base, p["id"]):
                stats["skip_easy"] += 1
                return [{"decision": "skip_easy"}]
        transcript = session_transcript(p, row, args.arm)
        if transcript is None:
            return []
        async with SLOTS.hold():                      # 提炼与入库(质量门、领域归类)也用同一个账号,占一个全局槽位
            return await store_lessons(engine, p, transcript, args.tag, args.arm, stats)

    for start in range(0, len(todo), args.concurrency):
        batch = todo[start:start + args.concurrency]
        primed = []
        for i, p in batch:
            results = []
            for attempt in range(4):                  # 检索 / 判官偶发失败:重试,仍失败就这题不注入(与 hook 静默放过一致)
                try:
                    results = await engine.match(p["problem"], max_results=HOOK_MAX, source="prime")
                    break
                except Exception as e:
                    log(f"    prime 失败({type(e).__name__}),重试 {attempt + 1}")
                    await asyncio.sleep(10 * (attempt + 1))
            picked = [r for r in results if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory][:HOOK_MAX]
            if picked:
                await engine.record_activations([r.enzyme.id for r in picked])
            primed.append([{"id": r.enzyme.id, "then": r.enzyme.then, "because": r.enzyme.because,
                            "severity": r.enzyme.severity.value, "confidence": r.enzyme.confidence,
                            "scope": "general" if "scope:general" in r.enzyme.tags else "specific"} for r in picked])
        outs = await asyncio.gather(*(solve(p, mems) for (i, p), mems in zip(batch, primed)))
        if args.arm == "reflect":
            log(f"  批 {start // args.concurrency + 1}:作答完成 {sum(bool(o.get('correct')) for o in outs)}/{len(outs)} 对")
            res = await asyncio.gather(*(reflect_prepare(p, out) for (i, p), out in zip(batch, outs)),
                                       return_exceptions=True)
            prepared.clear()
            prepared.update({p["id"]: ({"failed": f"error:{type(r).__name__}"} if isinstance(r, Exception) else r)
                             for (i, p), r in zip(batch, res)})
        for (i, p), mems, out in zip(batch, primed, outs):
            row = {"id": p["id"], "set": p["set"], "arm": args.arm, "pos": i, "injected": [m["id"] for m in mems],
                   "injected_scopes": [m["scope"] for m in mems], **out}
            if mems and outcome(row) is not None and args.arm != "seed":      # 未判定不得反馈或学习
                await engine.report_batch([m["id"] for m in mems], bool(row.get("correct")),
                                          detail=f"rsi: final answer {'verified correct' if row.get('correct') else 'wrong'}")
            try:
                row["lessons"] = await learn(p, row) if args.arm != "seed" else []
            except Exception as e:                    # 学习失败不影响作答记录
                row["lessons"] = [{"decision": f"error:{type(e).__name__}"}]
            await write(row)
            log(f"  [{i}] {p['set']:<11} {p['id']:<26} {'错误' if row.get('error') else ('对' if row.get('correct') else '错')} "
                f"注入 {len(mems)}(通用 {sum(m['scope'] == 'general' for m in mems)}) "
                f"经验 {[x.get('decision') for x in row['lessons']]} {row.get('secs')}s")
        if start // args.concurrency % 10 == 9:
            log(f"  进度 {start + len(batch)}/{len(todo)} · 记忆 {await engine.pg.count_enzymes()} · {dict(stats)}")
    await engine.drain(timeout=60)
    log(f"完成 · 记忆 {await engine.pg.count_enzymes()} · {dict(stats)}")
    await engine.pg.close()
    report(args.tag)


# ── 报表 ───────────────────────────────────────────────────────

def report(tag: str) -> str:
    meta = json.loads((OUT / f"{tag}.meta.json").read_text(encoding="utf-8"))
    order = meta["order"]
    problems = {p["id"]: p for p in json.loads((DATA / "problems.json").read_text(encoding="utf-8"))}
    stream = [problems[i] for i in order]
    rows = load_rows(tag)
    arms = [a for a in ARMS if any(k.startswith(f"{a}|") for k in rows)]
    dups = near_duplicates(stream)
    lines = [f"# hardbench 经验流:{tag}", "",
             f"集合 {', '.join(meta['sets'])} · 题目 {len(order)} · 种子 {meta['seed']} · 近重复题 {len(dups)}", ""]

    def ok(arm: str, pid: str) -> bool | None:
        r = rows.get(f"{arm}|{pid}")
        return outcome(r)

    lines += ["口径:判分失败/缺失与作答服务错误不计入正确率,配对只取两臂都有效的题。", "",
              "| 组 | 作答记录 | 有效判分 | 未判定 | 作答错误 |", "|---|---|---|---|---|"]
    for arm in arms:
        rs = [r for r in rows.values() if r["arm"] == arm]
        kinds = defaultdict(int)
        for r in rs:
            kinds[failure_kind(r)] += 1
        lines.append(f"| {arm} | {len(rs)} | {kinds['graded']} | {kinds['ungraded']} | {kinds['solver_error']} |")
    lines.append("")
    n_win = 5
    size = max(1, len(order) // n_win)
    lines += ["| 组 | 有效判分 | 正确率 | " + " | ".join(f"第 {k + 1} 段" for k in range(n_win)) + " |",
              "|---|---|---|" + "---|" * n_win]
    for arm in arms:
        vals = [ok(arm, pid) for pid in order]
        got = [v for v in vals if v is not None]
        segs = []
        for k in range(n_win):
            seg = [v for v in vals[k * size:(k + 1) * size if k < n_win - 1 else None] if v is not None]
            segs.append(f"{sum(seg) / len(seg):.0%}({len(seg)})" if seg else "—")
        lines.append(f"| {arm} | {len(got)} | {sum(got) / len(got):.1%} | " + " | ".join(segs) + " |" if got else f"| {arm} | 0 | — |")
    for arm in arms:
        if arm == "base":
            continue
        for label, ids in (("全部", order), ("后半程", order[len(order) // 2:]),
                           ("去掉近重复题", [i for i in order if i not in dups]),
                           ("有注入的题", [i for i in order if (rows.get(f"{arm}|{i}") or {}).get("injected")])):
            pairs = [(ok("base", i), ok(arm, i)) for i in ids]
            pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
            if not pairs:
                continue
            b = sum((not x) and y for x, y in pairs)
            c = sum(x and not y for x, y in pairs)
            acc_base = sum(x for x, _ in pairs) / len(pairs)
            acc_arm = sum(y for _, y in pairs) / len(pairs)
            lines.append(f"- {arm} vs base · {label}:{len(pairs)} 题,{acc_base:.1%} → {acc_arm:.1%}"
                         f"(变对 {b},变错 {c},McNemar p={mcnemar_p(b, c):.3f})"
                         + (f" ← 噪声底线:两次闭卷之间翻转 {b + c} 题({(b + c) / len(pairs):.1%})"
                            if arm == "base2" else ""))
        if arm == "base2":
            continue
        lessons = [x for r in rows.values() if r["arm"] == arm for x in (r.get("lessons") or [])]
        dec = defaultdict(int)
        for x in lessons:
            dec[x.get("decision")] += 1
        inj = [len(r.get("injected") or []) for r in rows.values() if r["arm"] == arm and not r.get("error")]
        scopes = defaultdict(int)
        for x in lessons:
            scopes[x.get("scope", "?")] += 1
        injected_general = sum(s == "general" for r in rows.values() if r["arm"] == arm
                               for s in (r.get("injected_scopes") or []))
        lines.append(f"- {arm} 经验:提炼 {len(lessons)} 条(通用方法论 {scopes['general']},领域方法 {scopes['specific']})· "
                     + ", ".join(f"{k} {v}" for k, v in sorted(dec.items(), key=str))
                     + f" · 平均每题注入 {sum(inj) / max(1, len(inj)):.2f} 条(其中通用 {injected_general} 次),"
                       f"有注入的题 {sum(1 for x in inj if x)}")

    # 伤害率 = base 做对的题里被弄错的比例,挽救率 = base 做错的题里被救回的比例:
    # 按 base 的对错分开算,题目难度不同的组之间也能比;base2 那行就是两次闭卷之间的纯随机翻转
    def rates(arm: str, ids) -> str | None:
        pairs = [(ok("base", i), ok(arm, i)) for i in ids]
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        if not pairs:
            return None
        right = [y for x, y in pairs if x]
        wrong = [y for x, y in pairs if not x]
        b, c = sum(wrong), len(right) - sum(right)
        return (f"{len(pairs)} | {sum(x for x, _ in pairs) / len(pairs):.1%} → {sum(y for _, y in pairs) / len(pairs):.1%} | "
                f"{c}/{len(right)} = {c / max(1, len(right)):.1%} | {b}/{len(wrong)} = {b / max(1, len(wrong)):.1%} | "
                f"{mcnemar_p(b, c):.3f}")

    head = ["| {} | 题数 | 正确率 base → 本组 | 伤害率(base 对 → 错) | 挽救率(base 错 → 对) | McNemar p |",
            "|---|---|---|---|---|---|"]
    lines += ["", "## 伤害率与挽救率(相对 base)", "", head[0].format("组"), head[1]]
    for arm in arms:
        if arm != "base" and (r := rates(arm, order)):
            lines.append(f"| {arm}{' (噪声底线)' if arm == 'base2' else ''} | {r} |")

    # 学习曲线:RSI 的标志是"经验越积累提升越大"——后半程相对 base 的提升应高于前半程,且超过 base2 的噪声
    lines += ["", "## 学习曲线(按题序四等分,本组相对 base 的正确率差,百分点)", "",
              "| 组 | 第 1 段 | 第 2 段 | 第 3 段 | 第 4 段 | 后半 − 前半 |", "|---|---|---|---|---|---|"]
    q = len(order) // 4
    quarters = [order[k * q:(k + 1) * q if k < 3 else None] for k in range(4)]
    for arm in arms:
        if arm == "base":
            continue
        cells = []
        for ids in quarters:
            pr = [(ok("base", i), ok(arm, i)) for i in ids]
            pr = [(a, b) for a, b in pr if a is not None and b is not None]
            cells.append(100 * (sum(b for _, b in pr) - sum(a for a, _ in pr)) / len(pr) if pr else None)
        if all(c is None for c in cells):
            continue
        fmt = [f"{c:+.1f}" if c is not None else "—" for c in cells]
        trend = f"{(cells[2] + cells[3] - cells[0] - cells[1]) / 2:+.1f}" if None not in cells else "—"
        lines.append(f"| {arm}{' (噪声)' if arm == 'base2' else ''} | {' | '.join(fmt)} | {trend} |")

    for hard, plain, label in (("self_hard", "self_v2", "同判官同并发,唯一差别是过滤"),
                               ("self_hard", "self", "运行条件不同(判官、并发),仅供参考"),
                               ("teacher_hard", "teacher", "运行条件不同(判官、并发),仅供参考"),
                               ("self_v3", "self_v2", "判官修复对失灵(其余同条件)"),
                               ("teacher_v3", "teacher", "判官修复对失灵(其余同条件)")):
        common = [i for i in order if None not in (ok("base", i), ok(hard, i), ok(plain, i))]
        if not common:
            continue
        b = sum((not ok(plain, i)) and ok(hard, i) for i in common)
        c = sum(ok(plain, i) and not ok(hard, i) for i in common)

        def inj(arm: str) -> float:
            return sum(len(rows[f"{arm}|{i}"].get("injected") or []) for i in common) / len(common)

        def added(arm: str) -> int:
            return sum(str(x.get("decision", "")).startswith("add") for r in rows.values() if r["arm"] == arm
                       for x in r.get("lessons") or [])
        lines += ["", f"## {hard} vs {plain}(同一批 {len(common)} 题,{label})", "",
                  head[0].format("组"), head[1],
                  f"| {plain} | {rates(plain, common)} |", f"| {hard} | {rates(hard, common)} |", "",
                  f"- 直接配对:{hard} 对 {plain} 变对 {b},变错 {c},McNemar p={mcnemar_p(b, c):.3f}",
                  f"- 每题注入 {plain} {inj(plain):.2f} 条 · {hard} {inj(hard):.2f} 条;"
                  f"新增入库 {plain} {added(plain)} 次 · {hard} {added(hard)} 次(全程累计)"]

    # 来源归因:不过滤的组里,每次注入的记忆是从哪类题学来的——
    # 易源 = 只学自 base 做对的题(过滤会拦掉的那类),难源 = 至少有一次学自 base 做错的题
    base_ok = {i: ok("base", i) for i in order}
    for arm in [a for a in ("self", "self_v2", "self_v3", "teacher", "teacher_v3", "seed_self") if a in arms]:
        arm_rows = sorted((r for r in rows.values() if r["arm"] == arm), key=lambda r: r["pos"])
        src = defaultdict(list)                       # 记忆 id → [(学自第几题, 那题 base 是否做对)]
        for r in arm_rows:
            for x in r.get("lessons") or []:
                for eid in x.get("ids") or []:
                    src[eid].append((r["pos"], base_ok.get(r["id"])))
        buckets = defaultdict(list)
        for r in arm_rows:
            if r.get("error"):
                continue
            kinds = set()
            for eid in r.get("injected") or []:
                before = [e for p, e in src.get(eid, []) if p < r["pos"] and e is not None]
                kinds.add("seed" if not before else "hard" if not all(before) else "easy")
            key = ("无注入" if not kinds else "含难源" if "hard" in kinds
                   else "仅易源(可含种子)" if "easy" in kinds else "仅种子/来源不明")
            buckets[key].append(r["id"])
        lines += ["", f"## 来源归因:{arm}", "", head[0].format("注入了什么"), head[1]]
        for key in ("含难源", "仅易源(可含种子)", "仅种子/来源不明", "无注入"):
            if buckets[key] and (r := rates(arm, buckets[key])):
                lines.append(f"| {key} | {r} |")

    by_set = defaultdict(lambda: defaultdict(list))
    for r in rows.values():
        if outcome(r) is not None:
            by_set[r["set"]][r["arm"]].append(bool(r.get("correct")))
    lines += ["", "| 集合 | " + " | ".join(arms) + " |", "|---|" + "---|" * len(arms)]
    for s, d in sorted(by_set.items()):
        lines.append(f"| {s} | " + " | ".join(f"{sum(d[a]) / len(d[a]):.0%}({len(d[a])})" if d[a] else "—" for a in arms) + " |")
    text = "\n".join(lines)
    (OUT / f"{tag}.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--arm", choices=ARMS)
    ap.add_argument("--sets", default="physelite")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--source-db", default="cognitive_enzyme_test", help="读模型注册表与绑定的库(只读)")
    ap.add_argument("--seed-db", default="", help="seed / seed_self 组的起点:seed.py 建好的种子库")
    ap.add_argument("--model", default="zai-coding-cn/glm-5.3")
    ap.add_argument("--only", default="", choices=("", "base_wrong"), help="base_wrong:只做 base 闭卷做错的题")
    ap.add_argument("--learn-model", default="", help="引擎 LLM(提炼、撞库裁决、质量门、reflect 复盘)换成这个模型;空 = 用库里的绑定")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--key-from", default="cc-switch-zhipu-glm")
    ap.add_argument("--key-env", default="ZAI_CODING_CN_API_KEY")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--max-wait", type=float, default=6 * 3600.0)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        report(args.tag)
        return
    if not args.arm:
        raise SystemExit("--arm 必填(base / self / teacher)")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
