"""真实经验流上的自我改进(RSI)评测 —— prequential:按真实时间顺序,先判定、再反馈、再学习。

数据:realbench 构建的 stream_firefox.json(Mozilla 重复报告,按提交时间)/ stream_legal.json
(LePaRD 引用语境,按判决日期)。记忆库 = 构建出的多领域语料,与线上一样混在一起。

每条情境:
  1. engine.match → 按 hook 规则过滤得到注入集(与线上一致)
  2. 记分:正确记忆是否被注入;按"正确记忆此前在流里出现过没有"分成 familiar / novel 两组
  3. 反馈(模拟真实闭环):
     - 注入了正确记忆 → 被动评估"帮上了" → hebbian 成功(经验样本开时顺带记下情境)
     - 没注入 → 以 reveal 概率"事后得知"正确记忆(对应 capture 把会话经验归并到已有记忆的
       REINFORCE):hebbian 成功;经验样本开时把这个情境记为它的样本
     - 其余注入的记忆:neutral(exemplar_anti 组则记为反例经验)

对照组(arms):
  hebbian        —— 现状:只有置信度学习(CE_EXEMPLAR_LEARN=off)
  exemplar       —— + 正例经验样本(学习、召回、判定提示)
  exemplar_anti  —— + 反例经验:注入了但不是正确答案的记忆,把情境记为它的反例
  exemplar_analogy —— + 类比识别:Jev 认定当前情境与某条记忆过去适用过的情境是同一类情况就注入

稳定性:逐条写检查点(进度、随机数状态、已动过的记忆),中断后 --resume 从断点续跑;单条出错
重试 3 次仍失败则记为 error 继续;每条记录 Jev 状态 / 精排失败 / 耗时,汇总里统计回退次数。
输出直接写文件(不依赖 stdout 管道)。每个 arm 开始前把动过的记忆从语料原样恢复。

  CE_EVAL_DB=... CE_EVAL_CORPUS_DIR=... .venv-eval/bin/python -m tests.eval.realbench.stream_eval \\
      --stream legal --limit 400 [--resume]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "results"
HOOK_MIN_CONF = 0.25
MAX_RESULTS = 6

log = logging.getLogger("stream_eval")


def _injected(results) -> list[str]:
    return [r.enzyme.id for r in results if r.enzyme.confidence >= HOOK_MIN_CONF or r.exploratory]


async def _restore(engine, originals: dict, ids: set[str]) -> None:
    """把指定记忆恢复成语料原样(含清空经验样本),并同步向量索引。"""
    for eid in sorted(ids):
        e = originals.get(eid)
        if e is None:
            continue
        fresh = e.model_copy(deep=True)
        await engine.pg.upsert_enzyme(fresh)
        await engine.sync.sync_enzyme(fresh)


def _window_stats(rows: list[dict], size: int) -> list[dict]:
    out = []
    for k in range(0, len(rows), size):
        w = [r for r in rows[k:k + size] if not r.get("error")]
        fam = [r for r in w if r["familiar"]]
        out.append({"from": k, "n": len(w), "hit": round(sum(r["hit"] for r in w) / len(w), 3) if w else None,
                    "familiar_n": len(fam),
                    "familiar_hit": round(sum(r["hit"] for r in fam) / len(fam), 3) if fam else None})
    return out


def _summary(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]

    def rate(xs):
        return round(sum(r["hit"] for r in xs) / len(xs), 4) if xs else None
    fam = [r for r in ok if r["familiar"]]
    nov = [r for r in ok if not r["familiar"]]
    inj = sum(r["n_injected"] for r in ok)
    return {"n": len(ok), "errors": len(rows) - len(ok), "hit": rate(ok),
            "familiar_n": len(fam), "familiar_hit": rate(fam), "novel_n": len(nov), "novel_hit": rate(nov),
            "precision": round(sum(r["hit"] for r in ok) / inj, 4) if inj else None,
            "avg_injected": round(inj / len(ok), 2) if ok else None,
            "second_half_familiar_hit": rate([r for r in fam if r["i"] >= len(rows) // 2]),
            # 稳定性:回退/失败必须接近 0,否则结果混进了噪声
            "jev_not_ok": sum(1 for r in ok if r.get("jev_status") not in ("ok", None, "")),
            "rerank_failed": sum(1 for r in ok if r.get("rerank_failed")),
            "latency_p50": sorted(r["latency"] for r in ok)[len(ok) // 2] if ok else None}


def _diagnose(trace: dict, gold: str) -> dict:
    """漏推定位:正确记忆卡在哪一环(召回 / 精排 / Jev)。"""
    recalled = trace.get("recalled_ids", [])
    jev = {eid: (p, q) for eid, p, q in trace.get("jev", [])}
    top_votes = sorted(trace.get("exemplar_votes", {}).items(), key=lambda kv: -kv[1])[:3]
    top_hints = sorted(trace.get("hint_scores", {}).items(), key=lambda kv: -kv[1])[:3]
    # 离线模拟"经验识别"类规则要用:票数/提示分最高的几条候选(对错都要,才能算 Precision)及其 Jev 判定
    cand = {eid for eid, _ in top_votes + top_hints} | set(trace.get("dosed_ids", []))
    analogy = {eid: [round(ap, 3), round(aq, 3)] for eid, ap, aq in trace.get("analogy", [])}
    return {"top_hints": top_hints,
            "analogy": analogy,
            "analogy_none": round(trace["analogy_none"], 3) if trace.get("analogy_none") is not None else None,
            "gold_analog_q": analogy.get(gold, [None, None])[1],
            "cand_jev": {eid: [round(jev[eid][0], 3), round(jev[eid][1], 3)] for eid in cand if eid in jev},
            "gold_recall_rank": recalled.index(gold) + 1 if gold in recalled else None,
            "gold_hinted": gold in trace.get("hinted_ids", []),
            "gold_hint_score": trace.get("hint_scores", {}).get(gold),
            "gold_votes": trace.get("exemplar_votes", {}).get(gold, 0),
            "top_votes": top_votes,
            "gold_reranked": gold in trace.get("reranked_ids", []),
            "gold_lexical": gold in trace.get("lexical_ids", []),
            "gold_p": round(jev[gold][0], 3) if gold in jev else None,
            "gold_q": round(jev[gold][1], 3) if gold in jev else None,
            "jev_none": round(trace["jev_none"], 3) if trace.get("jev_none") is not None else None}


def _ckpt_path(stream: str, limit: int, reveal: float, arm: str, tag: str = "") -> Path:
    return OUT / f"ckpt_{stream}_{limit}_r{reveal}{tag}_{arm}.json"


async def _match_with_retry(engine, text: str, trace: dict):
    for attempt in range(3):
        try:
            return await engine.match(text, max_results=MAX_RESULTS, trace=trace, source="stream")
        except Exception as e:
            log.warning("match failed (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(2 * (attempt + 1))
    return None


async def run_arm(engine, stream: list[dict], arm: str, reveal: float, seed: int, ckpt: Path,
                  resume: bool) -> dict:
    learn = arm.startswith("exemplar")
    os.environ["CE_EXEMPLAR_LEARN"] = "on" if learn else "off"
    os.environ["CE_EXEMPLAR_RECALL"] = "on" if learn else "off"
    os.environ["CE_ANTI_EXEMPLAR"] = "on" if arm == "exemplar_anti" else "off"
    # 类比识别:exemplar_analogy 组入选;其余学经验的组只问不用(shadow),留离线模拟的数据
    os.environ["CE_EXEMPLAR_ANALOGY"] = "on" if arm == "exemplar_analogy" else ("shadow" if learn else "off")
    rng = random.Random(seed)
    rows: list[dict] = []
    seen: set[str] = set()
    touched: set[str] = set()
    if resume and ckpt.exists():
        st = json.loads(ckpt.read_text(encoding="utf-8"))
        rows, seen, touched = st["rows"], set(st["seen"]), set(st["touched"])
        rng.setstate(tuple(st["rng"][0:1]) + (tuple(st["rng"][1]),) + tuple(st["rng"][2:]))
        log.info("[%s] resumed at %d/%d", arm, len(rows), len(stream))
    t0 = time.perf_counter()
    for i in range(len(rows), len(stream)):
        q = stream[i]
        gold = q["expected_ids"][0]
        trace: dict = {}
        t1 = time.perf_counter()
        results = await _match_with_retry(engine, q["text"], trace)
        if results is None:
            rows.append({"i": i, "id": q["id"], "error": True, "familiar": gold in seen, "hit": False,
                         "n_injected": 0, "latency": 0})
        else:
            injected = _injected(results)
            hit = gold in injected
            if trace.get("decision_id"):   # 判定记录挂上标准答案(on-policy 数据:这次判定由当前策略走出)
                await engine.record_outcome(gold, "gold", "stream", decision_id=trace["decision_id"])
            rows.append({"i": i, "id": q["id"], "hit": hit, "familiar": gold in seen,
                         "n_injected": len(injected), "latency": round(time.perf_counter() - t1, 2),
                         "jev_status": trace.get("jev_status", ""), "rerank_failed": bool(trace.get("rerank_failed")),
                         "injected": injected, **_diagnose(trace, gold)})
            try:
                if hit:
                    await engine.hebbian_update(gold, success=True, expected=True, detail="stream: helped")
                    touched.add(gold)
                elif rng.random() < reveal:
                    await engine.hebbian_update(gold, success=True, expected=True, detail="stream: revealed")
                    await engine.learn_exemplar(gold, q["text"], source="stream_reveal")
                    touched.add(gold)
                if arm == "exemplar_anti":
                    for eid in injected:
                        if eid != gold and eid not in q.get("acceptable_ids", []):
                            await engine.learn_exemplar(eid, q["text"], source="stream_irrelevant", kind="anti")
                            touched.add(eid)
            except Exception as e:
                log.warning("feedback failed at %d: %s", i, e)
        seen.add(gold)
        st = rng.getstate()
        ckpt.write_text(json.dumps({"rows": rows, "seen": sorted(seen), "touched": sorted(touched),
                                    "rng": [st[0], list(st[1]), st[2]]}, ensure_ascii=False), encoding="utf-8")
        if (i + 1) % 25 == 0:
            s = _summary(rows)
            log.info("[%s] %d/%d hit=%s familiar=%s(%d) novel=%s jev_not_ok=%d rerank_failed=%d %.0fs",
                     arm, i + 1, len(stream), s["hit"], s["familiar_hit"], s["familiar_n"], s["novel_hit"],
                     s["jev_not_ok"], s["rerank_failed"], time.perf_counter() - t0)
    return {"arm": arm, "summary": _summary(rows), "windows": _window_stats(rows, 100), "rows": rows,
            "touched": sorted(touched)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", choices=("firefox", "legal"), required=True)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--reveal", type=float, default=0.5, help="漏推后事后得知正确记忆的概率")
    ap.add_argument("--arms", default="hebbian,exemplar,exemplar_anti")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--resume", action="store_true", help="从检查点续跑(已完成的 arm 直接复用结果)")
    ap.add_argument("--tag", default="", help="结果/检查点文件名后缀(区分诊断重跑等)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    base = f"stream_{args.stream}_{args.limit}_r{args.reveal}{args.tag}"
    log_path = OUT / f"{base}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8")])
    for noisy in ("httpx", "httpcore", "app", "qdrant_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from app.core.engine import Engine
    import app.matching.pipeline as pipeline
    from tests.eval.bootstrap import eval_stores
    from tests.eval.validate import CORPUS_DIR, load_corpus

    pipeline._EXPLORATION_RATE = 0.0
    config, pg, qdrant = await eval_stores()
    engine = Engine(config, pg, qdrant)
    originals = {e.id: e for e in load_corpus(CORPUS_DIR)}
    stream = json.loads((HERE / "data" / f"stream_{args.stream}.json").read_text(encoding="utf-8"))["queries"]
    stream = [q for q in stream if q["expected_ids"][0] in originals][: args.limit]
    gold_ids = {q["expected_ids"][0] for q in stream}
    log.info("stream=%s items=%d memories=%d reveal=%s", args.stream, len(stream), len(originals), args.reveal)
    log.info("knobs %s", {k: os.environ.get(k, "") for k in (
        "CE_JEV_PROMPT", "CE_LEXICAL_RECALL", "CE_QUERY_EXPAND", "CE_EXEMPLAR_ANALOGY",
        "CE_JEV_CHOICE_OVERRIDE", "CE_JEV_RESCUE_Q", "CE_ANALOGY_Q", "CE_ANALOGY_P", "CE_ANALOGY_K")})

    res_path = OUT / f"{base}.json"
    results = json.loads(res_path.read_text(encoding="utf-8")) if (args.resume and res_path.exists()) else {}
    for arm in args.arms.split(","):
        if arm in results:
            log.info("[%s] already done, skipping", arm)
            continue
        ckpt = _ckpt_path(args.stream, args.limit, args.reveal, arm, args.tag)
        resuming = args.resume and ckpt.exists()
        if not resuming:
            # 干净起跑:流里所有正确记忆 + 其它 arm 检查点记录的动过的记忆,一律恢复原样
            prior = set()
            for other in OUT.glob(f"ckpt_{args.stream}_{args.limit}_r{args.reveal}{args.tag}_*.json"):
                prior |= set(json.loads(other.read_text(encoding="utf-8")).get("touched", []))
            await _restore(engine, originals, gold_ids | prior)
        res = await run_arm(engine, stream, arm, args.reveal, args.seed, ckpt, resuming)
        res.pop("touched")
        results[arm] = res
        log.info("%s %s", arm, json.dumps(res["summary"], ensure_ascii=False))
        res_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    prior = set()
    for other in OUT.glob(f"ckpt_{args.stream}_{args.limit}_r{args.reveal}{args.tag}_*.json"):
        prior |= set(json.loads(other.read_text(encoding="utf-8")).get("touched", []))
    await _restore(engine, originals, gold_ids | prior)
    log.info("DONE")
    await engine.drain(timeout=60)
    await pg.close()


if __name__ == "__main__":
    asyncio.run(main())
