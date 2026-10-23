"""Jev 适用性判定实验 —— 给其他 agent 注入记忆时,能否用 Jev(TypeSafe System One)
的校准概率来决定"推哪些、推几条"。纯离线,只调 TypeSafe API,不碰 PG/Qdrant。

两组数据:
  golden    : 现有召回评测集(250 酶 / 130 查询)。对照组 = reports/final.outcomes.jsonl
              里真实链路的注入结果。候选池两档夹逼:
                real-ish = 真实注入 ∪ 真实进入过精排后池子的 expected(归因 hit/dosage_cut)
                hard     = real-ish ∪ 字符二元组 TF-IDF top-10 近邻(额外干扰项)
  olympiad  : 合成奥数集(22 条技巧记忆 / 32 题 + 10 条静默),中英各一版,候选池=全部记忆。

用法:
  TYPESAFE_API_KEY=... python3 tests/eval/jev_gate/run_jev_gate.py
结果:tests/eval/jev_gate/out/{cache.jsonl,results.json,report.md}
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE.parent
OUT = HERE / "out"
API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")  # 钉版本,别让别名漂移污染对比
CAP = 6            # hook 硬顶(Recall@6)
POOL_TFIDF_K = 10
WORKERS = int(os.environ.get("JEV_WORKERS", "6"))
TAUS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

PROMPTS = {
    "zh": {
        "keys": ("触发", "建议", "依据"),
        "question": "`rule` 的建议对处理 state 中这个请求是否直接有用?",
        "true": "请求所处的场景正是该规则针对的场景,agent 照做会把事情做得更好",
        "false": "场景不同、只是关键词相似,或规则与请求无关",
    },
    "en": {
        "keys": ("trigger", "advice", "rationale"),
        "question": "Is the advice in `rule` directly useful for handling the request in state?",
        "true": "The request is exactly the situation the rule targets; following it would help",
        "false": "Different situation, only superficially similar keywords, or unrelated",
    },
}


# ── TypeSafe 调用(带缓存/重试) ────────────────────────────────

_cache_lock = threading.Lock()


def _load_cache() -> dict:
    cache = {}
    path = OUT / "cache.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8"):
            row = json.loads(line)
            cache[row["key"]] = row
    return cache


def call_jev(payload: dict, cache: dict, api_key: str) -> dict:
    key = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if key in cache:
        return cache[key]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = ""
    for attempt in range(5):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            row = {"key": key, "latency": time.perf_counter() - t0,
                   "n_questions": len(payload["questions"]), "response": data}
            with _cache_lock:
                cache[key] = row
                with (OUT / "cache.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:300]!r}"
            if e.code not in (429, 529) and e.code < 500:
                break
            time.sleep(min(2 ** attempt, 20))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = str(e)
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"TypeSafe call failed: {last_err}")


def build_payload(state: str, memories: list[dict], lang: str) -> dict:
    p = PROMPTS[lang]
    k_trig, k_adv, k_why = p["keys"]
    questions = {}
    for i, m in enumerate(memories):
        questions[f"c{i}"] = {
            "type": "noul",
            "instructions": {
                "rule": {k_trig: m["trigger"], k_adv: m["advice"], k_why: m["rationale"]},
                "question": p["question"],
            },
            "criteria": {"true": p["true"], "false": p["false"]},
        }
    return {"model": MODEL, "state": state, "questions": questions}


def score_pool(state: str, pool: list[str], mem: dict, lang: str, cache: dict, key: str) -> tuple[dict, dict]:
    if not pool:
        return {}, {}
    payload = build_payload(state, [mem[i] for i in pool], lang)
    row = call_jev(payload, cache, key)
    answers = row["response"]["answers"]
    probs = {mid: float(answers[f"c{i}"]["noul"]) for i, mid in enumerate(pool)}
    meta = {"latency": row["latency"], "n": len(pool),
            "input_tokens": row["response"].get("usage", {}).get("input_tokens", 0)}
    return probs, meta


# ── 词法检索(TF-IDF 字符二元组,作为干扰项来源与弱基线) ──────────

def _tokens(text: str) -> list[str]:
    text = text.lower()
    toks = re.findall(r"[a-z0-9]+", text)
    han = re.findall(r"[一-鿿]+", text)
    for run in han:
        toks += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return toks


class TfIdf:
    def __init__(self, docs: dict[str, str]):
        self.ids = list(docs)
        tfs = {i: Counter(_tokens(t)) for i, t in docs.items()}
        df = Counter(tok for tf in tfs.values() for tok in tf)
        n = len(docs)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.vecs = {i: self._vec(tf) for i, tf in tfs.items()}

    def _vec(self, tf: Counter) -> dict:
        v = {t: c * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def scores(self, query: str) -> dict[str, float]:
        q = self._vec(Counter(_tokens(query)))
        return {i: sum(q.get(t, 0.0) * x for t, x in v.items()) for i, v in self.vecs.items()}

    def top(self, query: str, k: int) -> list[str]:
        s = self.scores(query)
        return sorted(s, key=s.get, reverse=True)[:k]


# ── 指标 ────────────────────────────────────────────────────────

def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def calibration(pairs: list[tuple[float, bool]]) -> dict:
    edges = [0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0001]
    bins = []
    ece = 0.0
    for lo, hi in zip(edges, edges[1:]):
        b = [(p, y) for p, y in pairs if lo <= p < hi]
        if not b:
            bins.append({"range": f"{lo:.1f}-{min(hi, 1):.1f}", "n": 0})
            continue
        mp = statistics.mean(p for p, _ in b)
        fr = sum(y for _, y in b) / len(b)
        ece += len(b) / len(pairs) * abs(mp - fr)
        bins.append({"range": f"{lo:.1f}-{min(hi, 1):.1f}", "n": len(b),
                     "mean_p": round(mp, 3), "frac_relevant": round(fr, 3)})
    brier = statistics.mean((p - y) ** 2 for p, y in pairs) if pairs else None
    return {"bins": bins, "ece": round(ece, 4), "brier": round(brier, 4) if brier is not None else None}


def agg(rows: list[dict]) -> dict:
    """rows: {kind, expected, acceptable, injected} —— 口径与 tests/eval/metrics._agg 一致。"""
    pos = [r for r in rows if r["kind"] == "positive"]
    sil = [r for r in rows if r["kind"] == "silent"]
    exp_total = sum(len(r["expected"]) for r in pos)
    exp_hit = sum(len(set(r["expected"]) & set(r["injected"])) for r in pos)
    inj_total = sum(len(r["injected"]) for r in pos)
    inj_rel = sum(len(set(r["injected"]) & (set(r["expected"]) | set(r["acceptable"]))) for r in pos)
    rr = []
    for r in pos:
        rank = next((i + 1 for i, x in enumerate(r["injected"]) if x in r["expected"]), None)
        rr.append(1 / rank if rank else 0.0)
    return {
        "recall": round(exp_hit / exp_total, 4) if exp_total else None,
        "precision": round(inj_rel / inj_total, 4) if inj_total else 1.0,
        "silence": round(sum(1 for r in sil if not r["injected"]) / len(sil), 4) if sil else None,
        "mrr": round(sum(rr) / len(rr), 4) if rr else None,
        "avg_k_pos": round(inj_total / len(pos), 2) if pos else None,
    }


def vark(pool_probs: dict[str, float], tau: float, cap: int = CAP) -> list[str]:
    ranked = sorted(pool_probs, key=pool_probs.get, reverse=True)
    return [m for m in ranked if pool_probs[m] >= tau][:cap]


# ── 数据集:golden ──────────────────────────────────────────────

def load_golden():
    mem = {}
    for f in sorted((EVAL / "corpus").glob("*.json")):
        for e in json.load(f.open(encoding="utf-8")):
            mem[e["id"]] = {
                "trigger": ";".join(p["value"] for p in e.get("patterns", [])),
                "advice": e["then"], "rationale": e.get("because", ""),
                "domain": e.get("domain", ""),
            }
    queries = json.load((EVAL / "queries.json").open(encoding="utf-8"))["queries"]
    outcomes = {}
    for line in (EVAL / "reports" / "final.outcomes.jsonl").open(encoding="utf-8"):
        o = json.loads(line)
        outcomes[o["query_id"]] = o
    return mem, queries, outcomes


def run_golden(cache: dict, key: str) -> dict:
    mem, queries, outcomes = load_golden()
    tfidf = TfIdf({i: f"{m['trigger']} {m['advice']} {m['rationale']}" for i, m in mem.items()})

    jobs = []
    for q in queries:
        o = outcomes[q["id"]]
        in_pool_exp = [e for e, st in o["attribution"].items()
                       if st in ("hit", "dosage_cut", "hook_conf_filtered")]
        realish = list(dict.fromkeys(o["injected"] + in_pool_exp))
        hard = list(dict.fromkeys(realish + tfidf.top(q["text"], POOL_TFIDF_K)))
        jobs.append((q, o, realish, hard))

    def work(job):
        q, o, realish, hard = job
        probs, meta = score_pool(q["text"], hard, mem, "zh", cache, key)
        return q, o, realish, hard, probs, meta

    with ThreadPoolExecutor(WORKERS) as ex:
        done = list(ex.map(work, jobs))

    per_query = []
    for q, o, realish, hard, probs, meta in done:
        per_query.append({
            "id": q["id"], "kind": q["kind"], "domain": q["domain"],
            "expected": q.get("expected_ids", []), "acceptable": q.get("acceptable_ids", []),
            "baseline_injected": o["injected"], "attribution": o["attribution"],
            "pool_realish": realish, "pool_hard": hard, "p": probs, "meta": meta,
        })

    def rel(r, m):
        return m in set(r["expected"]) | set(r["acceptable"])

    pairs = [(r["p"][m], rel(r, m)) for r in per_query for m in r["pool_hard"]]
    by_domain = {}
    for d in sorted({r["domain"] for r in per_query}):
        rs = [r for r in per_query if r["domain"] == d and r["kind"] == "positive"]
        pos = [r["p"][m] for r in rs for m in r["pool_hard"] if rel(r, m)]
        neg = [r["p"][m] for r in rs for m in r["pool_hard"] if not rel(r, m)]
        by_domain[d] = {"auc": round(auc(pos, neg) or 0, 4), "n_pos": len(pos), "n_neg": len(neg)}

    # 对"真实链路注入过的错误项"单看:Jev 能不能把它们判低
    inj_pairs = [(r["p"][m], rel(r, m)) for r in per_query if r["kind"] == "positive"
                 for m in r["baseline_injected"]]

    base = agg([{**r, "injected": r["baseline_injected"]} for r in per_query])
    sims = {"baseline(final)": base}
    for tau in TAUS:
        sims[f"gate τ={tau}"] = agg([{**r, "injected": [m for m in r["baseline_injected"] if r["p"][m] >= tau]}
                                    for r in per_query])
    for tau in TAUS:
        sims[f"vark-realish τ={tau}"] = agg([{**r, "injected": vark({m: r["p"][m] for m in r["pool_realish"]}, tau)}
                                            for r in per_query])
    for tau in TAUS:
        sims[f"vark-hard τ={tau}"] = agg([{**r, "injected": vark({m: r["p"][m] for m in r["pool_hard"]}, tau)}
                                         for r in per_query])

    return {
        "per_query": per_query,
        "auc_all": round(auc([p for p, y in pairs if y], [p for p, y in pairs if not y]) or 0, 4),
        "auc_on_baseline_injected": round(auc([p for p, y in inj_pairs if y], [p for p, y in inj_pairs if not y]) or 0, 4),
        "calibration": calibration(pairs),
        "by_domain": by_domain,
        "sims": sims,
        "latency": _latency([r["meta"] for r in per_query if r["meta"]]),
    }


# ── 数据集:olympiad(中英对照) ────────────────────────────────

def run_olympiad(cache: dict, key: str) -> dict:
    data = json.load((HERE / "olympiad.json").open(encoding="utf-8"))
    out = {}
    for lang in ("zh", "en"):
        mem = {m["id"]: m[lang] for m in data["memories"]}
        ids = list(mem)
        tfidf = TfIdf({i: f"{m['trigger']} {m['advice']} {m['rationale']}" for i, m in mem.items()})

        def work(q):
            probs, meta = score_pool(q[lang], ids, mem, lang, cache, key)
            return q, probs, meta

        with ThreadPoolExecutor(WORKERS) as ex:
            done = list(ex.map(work, data["queries"]))

        per_query, pairs, lex_pairs = [], [], []
        top1 = top1_lex = npos = 0
        for q, probs, meta in done:
            relset = set(q["expected"]) | set(q["acceptable"])
            lex = tfidf.scores(q[lang])
            for m in ids:
                pairs.append((probs[m], m in relset))
                lex_pairs.append((lex[m], m in relset))
            if q["kind"] == "positive":
                npos += 1
                top1 += max(probs, key=probs.get) in relset
                top1_lex += max(lex, key=lex.get) in relset
            per_query.append({"id": q["id"], "kind": q["kind"], "expected": q["expected"],
                              "acceptable": q["acceptable"], "p": probs, "meta": meta,
                              "top3": sorted(probs, key=probs.get, reverse=True)[:3]})

        sims = {}
        for k in (1, 3, 5):
            sims[f"lexical top-{k}"] = agg([{**r, "injected": sorted(ids, key=tfidf.scores(
                next(q[lang] for q in data["queries"] if q["id"] == r["id"])).get, reverse=True)[:k]}
                for r in per_query])
        for tau in TAUS:
            sims[f"vark τ={tau}"] = agg([{**r, "injected": vark(r["p"], tau)} for r in per_query])

        out[lang] = {
            "per_query": per_query,
            "auc": round(auc([p for p, y in pairs if y], [p for p, y in pairs if not y]) or 0, 4),
            "auc_lexical": round(auc([p for p, y in lex_pairs if y], [p for p, y in lex_pairs if not y]) or 0, 4),
            "top1_acc": round(top1 / npos, 4),
            "top1_acc_lexical": round(top1_lex / npos, 4),
            "calibration": calibration(pairs),
            "sims": sims,
            "latency": _latency([r["meta"] for r in per_query]),
        }
    return out


def _latency(metas: list[dict]) -> dict:
    lat = sorted(m["latency"] for m in metas)
    if not lat:
        return {}
    return {"n": len(lat), "p50": round(statistics.median(lat), 3),
            "p90": round(lat[int(0.9 * (len(lat) - 1))], 3), "max": round(lat[-1], 3),
            "avg_questions": round(statistics.mean(m["n"] for m in metas), 1),
            "avg_input_tokens": round(statistics.mean(m["input_tokens"] for m in metas))}


def main() -> None:
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        sys.exit("TYPESAFE_API_KEY 未设置")
    OUT.mkdir(exist_ok=True)
    cache = _load_cache()
    which = sys.argv[1:] or ["golden", "olympiad"]
    results = {"model": MODEL}
    if "olympiad" in which:
        results["olympiad"] = run_olympiad(cache, key)
    if "golden" in which:
        results["golden"] = run_golden(cache, key)
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "per_query"} if isinstance(v, dict) else v
                      for k, v in results.items()}, ensure_ascii=False, indent=1)[:20000])


if __name__ == "__main__":
    main()
