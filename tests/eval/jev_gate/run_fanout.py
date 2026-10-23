"""Noul + Choice 同请求 fan-out 实验:逐条独立的 noul 容易把多条近邻都判"有用",
加一道"哪条最有用 / 都没用"的 Choice 补上相对比较,且不增加请求数与延迟。

组合策略(阈值在代码里定,模型只给概率):
  noul        : 只看 noul p ≥ τ(基线)
  choice      : 只看 choice 概率 q ≥ τ
  noul×rel    : s = p · q/max(q)  —— 绝对适用性 × 相对优势
  noul∧none   : p ≥ τ 且 P(none) < 0.5(Choice 认为"都没用"就整体静默)

用法: TYPESAFE_API_KEY=... python3 tests/eval/jev_gate/run_fanout.py
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_public as rp  # noqa: E402
from run_jev_gate import MODEL, OUT, TAUS, _latency, _load_cache, auc, call_jev  # noqa: E402

CHOICE_Q = {
    "zh": ("以下规则中,哪一条对处理 state 中这个请求最直接有用?如果没有一条直接有用,选 none。",
           "没有任何一条规则对这个请求直接有用"),
    "en": ("Which rule is most directly useful for handling the request in state? "
           "If none is directly useful, choose none.",
           "No rule is directly useful for this request"),
}


def fanout_scores(task, items, mem, cache, key):
    spec = rp.TASKS[task]
    lang = spec["lang"]

    def one(it):
        qs = {}
        crit = {}
        for i, m in enumerate(it["pool"]):
            rule = {f: mem[m][f] for f in spec["fields"]}
            qs[f"c{i}"] = {"type": "noul",
                           "instructions": {"rule": rule, "question": spec["question"]},
                           "criteria": {"true": spec["true"], "false": spec["false"]}}
            crit[f"c{i}"] = rule
        crit["none"] = CHOICE_Q[lang][1]
        qs["best"] = {"type": "choice", "instructions": CHOICE_Q[lang][0], "criteria": crit}
        row = call_jev({"model": MODEL, "state": it["state"], "questions": qs}, cache, key)
        ans = row["response"]["answers"]
        p = {m: float(ans[f"c{i}"]["noul"]) for i, m in enumerate(it["pool"])}
        probs = ans["best"]["probabilities"]
        q = {m: float(probs.get(f"c{i}", 0.0)) for i, m in enumerate(it["pool"])}
        return p, q, float(probs.get("none", 0.0)), {
            "latency": row["latency"], "n": len(qs),
            "input_tokens": row["response"].get("usage", {}).get("input_tokens", 0)}

    with ThreadPoolExecutor(6) as ex:
        return list(ex.map(one, items))


def combos(p, q, none):
    qmax = max(q.values()) if q else 0.0
    rel = {m: (q[m] / qmax if qmax > 0 else 0.0) for m in p}
    return {
        "noul": p,
        "choice": q,
        "noul×rel": {m: p[m] * rel[m] for m in p},
        "noul∧none": {m: (p[m] if none < 0.5 else 0.0) for m in p},
        "noul×(1-none)": {m: p[m] * (1 - none) for m in p},
    }


def evaluate(items, scored):
    out = {}
    names = list(combos({"x": 0}, {"x": 0}, 0))
    for name in names:
        pairs, sims = [], {}
        per = []
        for it, (p, q, none, _) in zip(items, scored):
            s = combos(p, q, none)[name]
            per.append((it, s))
            pairs += [(s[m], m in it["gold"]) for m in it["pool"]]
        for tau in TAUS:
            hit = inj = rel = npos = sil_ok = sil_n = 0
            for it, s in per:
                sel = [m for m in sorted(it["pool"], key=s.get, reverse=True) if s[m] >= tau][:6]
                sil_n += 1
                if it["kind"] == "positive" and it["gold"]:
                    npos += 1
                    hit += bool(set(sel) & it["gold"])
                    inj += len(sel)
                    rel += len(set(sel) & it["gold"])
                    sil_ok += not [m for m in sel if m not in it["gold"]]
                else:
                    sil_ok += not sel
            sims[tau] = {"recall": round(hit / npos, 3), "precision": round(rel / inj, 3) if inj else 1.0,
                         "silence": round(sil_ok / sil_n, 3), "k": round(inj / npos, 2)}
        out[name] = {"auc": round(auc([x for x, y in pairs if y], [x for x, y in pairs if not y]), 4),
                     "sims": sims}
    out["_latency"] = _latency([m for *_, m in scored])
    return out


def main():
    key = os.environ["TYPESAFE_API_KEY"]
    cache = _load_cache()
    results = {}
    for task, builder in (("golden", rp.build_golden), ("theoremqa", rp.build_theoremqa),
                          ("metatool", rp.build_metatool)):
        items, mem, _ = builder()
        if task != "golden":
            items = rp._shuffle_items(items, rp.GLM_SUBSET)  # 与 GLM 对照同一子集
        results[task] = evaluate(items, fanout_scores(task, items, mem, cache, key))
        print(f"== {task} latency={results[task]['_latency']}")
        for name, r in results[task].items():
            if name.startswith("_"):
                continue
            best = max(r["sims"].items(), key=lambda kv: kv[1]["precision"] + kv[1]["recall"] + kv[1]["silence"])
            print(f"  {name:14s} AUC={r['auc']}  τ.5={r['sims'][0.5]}  τ.6={r['sims'][0.6]}  best(sum)={best}")
    (OUT / "fanout_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
