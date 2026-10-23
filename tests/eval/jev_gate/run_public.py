"""公开基准上的 Jev 适用性判定实验 + LLM(GLM)对照。

数据集(自动下载到 out/pub/):
  theoremqa : TheoremQA test(去掉带图题),记忆=定理(名称+描述),每题标注所需定理。
  metatool  : MetaTool all_clean_data,记忆=工具描述,按工具分层抽样 400 条。
  golden / olympiad_zh / olympiad_en : 复用 run_jev_gate 的本地集(仅供 GLM 对照)。

候选池:TF-IDF top-(POOL-1) ∪ 金标,打乱顺序(按 id 哈希,确定性)。
"留出静默":把金标从池里拿掉后,剩余候选的 p 是否全部低于 τ——对应"库里没有适用记忆时应不注入"。

用法:
  TYPESAFE_API_KEY=... GLM_API_KEY=... python3 tests/eval/jev_gate/run_public.py [theoremqa] [metatool] [glm]
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_jev_gate import (  # noqa: E402
    HERE, OUT, TAUS, TfIdf, _latency, _load_cache, auc, calibration, call_jev,
    load_golden, MODEL,
)

PUB = OUT / "pub"
POOL = 12
GLM_URL = os.environ.get("GLM_API_URL", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5")
GLM_SUBSET = int(os.environ.get("GLM_SUBSET", "150"))

SOURCES = {
    "tqa_test.json": "https://raw.githubusercontent.com/wenhuchen/TheoremQA/main/theoremqa_test.json",
    "all_theorems.json": "https://raw.githubusercontent.com/wenhuchen/TheoremQA/main/all_theorems.json",
    "mt_data.csv": "https://raw.githubusercontent.com/HowieHwong/MetaTool/master/dataset/data/all_clean_data.csv",
    "mt_des.json": "https://raw.githubusercontent.com/HowieHwong/MetaTool/master/dataset/plugin_des.json",
}

TASKS = {
    "theoremqa": {
        "lang": "en",
        "fields": ("name", "description"),
        "question": "Is the theorem or concept in `rule` needed to solve the problem in state?",
        "true": "Solving the problem directly requires applying this theorem or concept",
        "false": "The problem does not need this theorem; it is only loosely related or unrelated",
    },
    "metatool": {
        "lang": "en",
        "fields": ("name", "description"),
        "question": "Should the tool in `rule` be called to fulfil the request in state?",
        "true": "The request is exactly what this tool is for; calling it would directly fulfil the request",
        "false": "The tool does not serve this request, or is only loosely related",
    },
    "golden": {
        "lang": "zh", "fields": ("触发", "建议", "依据"),
        "question": "`rule` 的建议对处理 state 中这个请求是否直接有用?",
        "true": "请求所处的场景正是该规则针对的场景,agent 照做会把事情做得更好",
        "false": "场景不同、只是关键词相似,或规则与请求无关",
    },
}


def _download() -> None:
    PUB.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        path = PUB / name
        if not path.exists():
            urllib.request.urlretrieve(url, path)


def _shuffle(ids: list[str], seed: str) -> list[str]:
    ids = list(ids)
    random.Random(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)).shuffle(ids)
    return ids


# ── 数据集构造:返回 items=[{id, state, pool, gold:set, (kind)}], mem={id: {field: text}} ──

def build_theoremqa():
    th = {k.lower(): v for k, v in json.load((PUB / "all_theorems.json").open(encoding="utf-8")).items()}
    qs = [q for q in json.load((PUB / "tqa_test.json").open(encoding="utf-8")) if not q["Picture"]]
    mem = {k: {"name": k, "description": re.sub(r"\s+", " ", v)[:500]} for k, v in th.items()}
    tfidf = TfIdf({k: f"{m['name']} {m['description']}" for k, m in mem.items()})
    items = []
    for q in qs:
        gold = q["theorem"].lower()
        pool = [x for x in tfidf.top(q["Question"], POOL) if x != gold][:POOL - 1] + [gold]
        items.append({"id": q["id"], "state": q["Question"], "pool": _shuffle(pool, q["id"]),
                      "gold": {gold}, "kind": "positive"})
    return items, mem, tfidf


def build_metatool(n: int = 400):
    des = json.load((PUB / "mt_des.json").open(encoding="utf-8"))
    rows = list(csv.DictReader((PUB / "mt_data.csv").open(encoding="utf-8")))
    by_tool: dict[str, list[dict]] = {}
    for r in rows:
        if r["Tool"] in des:
            by_tool.setdefault(r["Tool"], []).append(r)
    rng = random.Random(0)
    tools = sorted(by_tool)
    picked = []
    while len(picked) < n:  # 按工具轮转抽样,避免 FinanceTool 之类大类垄断
        for t in tools:
            if by_tool[t] and len(picked) < n:
                picked.append(by_tool[t].pop(rng.randrange(len(by_tool[t]))))
    mem = {t: {"name": t, "description": d} for t, d in des.items()}
    tfidf = TfIdf({k: f"{m['name']} {m['description']}" for k, m in mem.items()})
    items = []
    for i, r in enumerate(picked):
        gold = r["Tool"]
        pool = [x for x in tfidf.top(r["Query"], POOL) if x != gold][:POOL - 1] + [gold]
        qid = f"mt{i:03d}"
        items.append({"id": qid, "state": r["Query"], "pool": _shuffle(pool, qid), "gold": {gold}, "kind": "positive"})
    return items, mem, tfidf


def build_golden():
    """复用 run_jev_gate 的 golden:困难池(真实注入 ∪ 池内 expected ∪ TF-IDF top-10)。"""
    mem0, queries, outcomes = load_golden()
    mem = {k: {"触发": m["trigger"], "建议": m["advice"], "依据": m["rationale"]} for k, m in mem0.items()}
    tfidf = TfIdf({i: f"{m['trigger']} {m['advice']} {m['rationale']}" for i, m in mem0.items()})
    items = []
    for q in queries:
        o = outcomes[q["id"]]
        in_pool = [e for e, st in o["attribution"].items() if st in ("hit", "dosage_cut", "hook_conf_filtered")]
        pool = list(dict.fromkeys(o["injected"] + in_pool + tfidf.top(q["text"], 10)))
        items.append({"id": q["id"], "state": q["text"], "pool": pool, "kind": q["kind"],
                      "gold": set(q.get("expected_ids", [])) | set(q.get("acceptable_ids", []))})
    return items, mem, tfidf


# ── 打分器 ─────────────────────────────────────────────────────

def jev_scores(task: str, items, mem, cache, key):
    spec = TASKS[task]

    def one(it):
        qs = {}
        for i, m in enumerate(it["pool"]):
            qs[f"c{i}"] = {"type": "noul",
                           "instructions": {"rule": {f: mem[m][f] for f in spec["fields"]},
                                            "question": spec["question"]},
                           "criteria": {"true": spec["true"], "false": spec["false"]}}
        row = call_jev({"model": MODEL, "state": it["state"], "questions": qs}, cache, key)
        ans = row["response"]["answers"]
        return ({m: float(ans[f"c{i}"]["noul"]) for i, m in enumerate(it["pool"])},
                {"latency": row["latency"], "n": len(it["pool"]),
                 "input_tokens": row["response"].get("usage", {}).get("input_tokens", 0)})

    with ThreadPoolExecutor(6) as ex:
        return list(ex.map(one, items))


_glm_lock = threading.Lock()


def glm_scores(task: str, items, mem, cache, key):
    spec = TASKS[task]
    zh = spec["lang"] == "zh"
    sys_prompt = ("你是相关性判定器。对每个候选,给出它对处理用户请求『直接有用』的概率(0 到 1 的小数)。"
                  "只输出 JSON 对象,键为候选编号,值为概率,不要任何其他文字。" if zh else
                  "You are a relevance judge. For each candidate, give the probability (0 to 1) that it is "
                  "directly useful for the user's request. Output only a JSON object mapping candidate ids to "
                  "probabilities, nothing else.")

    def one(it):
        cands = "\n".join(f"[c{i}] " + json.dumps({f: mem[m][f] for f in spec["fields"]}, ensure_ascii=False)
                          for i, m in enumerate(it["pool"]))
        crit = f"判定标准:{spec['true']} → 接近 1;{spec['false']} → 接近 0。" if zh else \
            f"Criteria: {spec['true']} → near 1; {spec['false']} → near 0."
        user = f"{spec['question']}\n{crit}\n\n# state\n{it['state']}\n\n# candidates\n{cands}"
        payload = {"model": GLM_MODEL, "temperature": 0, "max_tokens": 600,
                   "thinking": {"type": "disabled"},
                   "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]}
        ck = "glm:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if ck not in cache:
            body = json.dumps(payload, ensure_ascii=False).encode()
            err = ""
            for attempt in range(6):
                req = urllib.request.Request(GLM_URL, data=body, method="POST", headers={
                    "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
                t0 = time.perf_counter()
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        data = json.loads(resp.read())
                    row = {"key": ck, "latency": time.perf_counter() - t0, "response": data}
                    with _glm_lock:
                        cache[ck] = row
                        with (OUT / "cache.jsonl").open("a", encoding="utf-8") as f:
                            f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    break
                except urllib.error.HTTPError as e:
                    err = f"HTTP {e.code} {e.read()[:200]!r}"
                    time.sleep(min(3 * 2 ** attempt, 60))
                except (urllib.error.URLError, TimeoutError) as e:
                    err = str(e)
                    time.sleep(min(3 * 2 ** attempt, 60))
            else:
                return None, {"error": err}
        row = cache[ck]
        text = row["response"]["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            raw = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            raw = {}
        probs = {}
        for i, mid in enumerate(it["pool"]):
            try:
                probs[mid] = max(0.0, min(1.0, float(raw.get(f"c{i}", raw.get(f"[c{i}]", 0.0)))))
            except (TypeError, ValueError):
                probs[mid] = 0.0
        usage = row["response"].get("usage", {})
        return probs, {"latency": row["latency"], "n": len(it["pool"]),
                       "input_tokens": usage.get("prompt_tokens", 0), "parsed": bool(raw)}

    with ThreadPoolExecutor(3) as ex:
        return list(ex.map(one, items))


# ── 评估 ───────────────────────────────────────────────────────

def evaluate(items, scored, tfidf=None) -> dict:
    pairs, top1, top1_lex, npos = [], 0, 0, 0
    sims = {}
    rows = []
    for it, (probs, meta) in zip(items, scored):
        if probs is None:
            continue
        rows.append((it, probs, meta))
        for m in it["pool"]:
            pairs.append((probs[m], m in it["gold"]))
        if it["kind"] == "positive" and it["gold"]:
            npos += 1
            top1 += max(it["pool"], key=lambda m: probs[m]) in it["gold"]
            if tfidf is not None:
                lex = tfidf.scores(it["state"])
                top1_lex += max(it["pool"], key=lambda m: lex.get(m, 0)) in it["gold"]
    for tau in TAUS:
        hit = inj = rel = sil_ok = sil_n = 0
        for it, probs, _ in rows:
            sel = [m for m in sorted(it["pool"], key=probs.get, reverse=True) if probs[m] >= tau][:6]
            if it["kind"] == "positive" and it["gold"]:
                hit += bool(set(sel) & it["gold"])
                inj += len(sel)
                rel += len(set(sel) & it["gold"])
                # 留出静默:拿掉金标后是否一条都不推
                sil_n += 1
                sil_ok += not [m for m in sel if m not in it["gold"]]
            else:
                sil_n += 1
                sil_ok += not sel
        sims[f"τ={tau}"] = {"recall": round(hit / npos, 4) if npos else None,
                            "precision": round(rel / inj, 4) if inj else 1.0,
                            "silence_heldout": round(sil_ok / sil_n, 4) if sil_n else None,
                            "avg_k": round(inj / npos, 2) if npos else None}
    return {
        "n": len(rows),
        "auc": round(auc([p for p, y in pairs if y], [p for p, y in pairs if not y]) or 0, 4),
        "top1": round(top1 / npos, 4) if npos else None,
        "top1_lexical": round(top1_lex / npos, 4) if (npos and tfidf is not None) else None,
        "calibration": calibration(pairs),
        "sims": sims,
        "latency": _latency([m for _, _, m in rows]),
        "parse_fail": sum(1 for _, _, m in rows if m.get("parsed") is False),
    }


def main() -> None:
    which = sys.argv[1:] or ["theoremqa", "metatool", "glm"]
    ts_key = os.environ.get("TYPESAFE_API_KEY", "")
    glm_key = os.environ.get("GLM_API_KEY", "")
    _download()
    cache = _load_cache()
    res_path = OUT / "public_results.json"
    results = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}
    builders = {"theoremqa": build_theoremqa, "metatool": build_metatool, "golden": build_golden}

    for task in ("theoremqa", "metatool"):
        if task not in which:
            continue
        items, mem, tfidf = builders[task]()
        results.setdefault(task, {})["jev"] = evaluate(items, jev_scores(task, items, mem, cache, ts_key), tfidf)
        print(task, "jev", {k: v for k, v in results[task]["jev"].items() if k not in ("calibration", "sims")}, flush=True)

    if "glm" in which:
        for task in ("theoremqa", "metatool", "golden"):
            items, mem, tfidf = builders[task]()
            sub = items if task == "golden" else _shuffle_items(items, GLM_SUBSET)
            jev = jev_scores(task, sub, mem, cache, ts_key)
            glm = glm_scores(task, sub, mem, cache, glm_key)
            r = results.setdefault(task, {})
            r["subset_jev"] = evaluate(sub, jev, tfidf)
            r["subset_glm"] = evaluate(sub, glm, tfidf)
            print(task, "subset jev/glm auc", r["subset_jev"]["auc"], r["subset_glm"]["auc"], flush=True)

    res_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def _shuffle_items(items, n):
    rng = random.Random(42)
    return rng.sample(items, min(n, len(items)))


if __name__ == "__main__":
    main()
