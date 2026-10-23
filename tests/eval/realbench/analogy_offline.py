"""类比识别(离线,只调 Jev API):过去情境里找 BM25 最像的 3 条(各属不同记忆),问 Jev
"当前情境和这条过去情境是不是同一类情况、需要同一条知识",看能否既认出熟悉情境、又拒掉新情境。

过去情境按 reveal 概率随机"学会"(与经验流一致,默认 0.5;命中的不计,偏保守)。
  (real_env) .venv-eval/bin/python -m tests.eval.realbench.analogy_offline legal 400
  python3 tests/eval/realbench/analogy_offline.py report legal firefox
"""
import asyncio
import json
import math
import random
import sys
from collections import Counter

from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "data" / "results"

NOUL = "state 里的情境,和 `past` 这条过去的情境,是不是同一类情况——处理它们需要的是同一条知识?"
TRUE = "两者针对的是同一个具体问题、同一条规则或同一个已知缺陷,只是表述不同"
FALSE = "只是主题相近或用词相似,实际需要的知识不同"
CHOICE = "以下过去的情境中,哪一条和 state 里的情境是同一类情况、需要同一条知识?如果都不是,选 none。"


def bm25_top(query, docs, df, k=3):
    from app.matching.lexical import tokenize
    N = len(docs)
    avgdl = sum(len(d) for d in docs) / N
    tf = Counter(tokenize(query))
    sc = []
    for j, d in enumerate(docs):
        dtf = Counter(d["toks"])
        s = 0.0
        for w in tf:
            if w in dtf:
                idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                c = dtf[w]
                s += idf * c * 2.2 / (c + 1.2 * (0.25 + 0.75 * len(d["toks"]) / avgdl))
        sc.append((s, j))
    sc.sort(reverse=True)
    out, seen = [], set()
    for s, j in sc:
        g = docs[j]["gold"]
        if g in seen:
            continue
        seen.add(g)
        out.append((s, docs[j]))
        if len(out) == k:
            break
    return out


async def main(name: str, limit: int, reveal: float):
    from app.config import Config
    from app.matching.lexical import tokenize
    from app.typesafe_client import system_one
    cfg = Config.from_env().typesafe
    out_path = RESULTS / f"analogy_{name}.jsonl"
    stream = json.load(open(HERE / "data" / f"stream_{name}.json"))["queries"][:limit]
    rng = random.Random(0)
    learned, df = [], Counter()
    done = set()
    try:
        done = {json.loads(line)["i"] for line in open(out_path)}
    except FileNotFoundError:
        pass
    seen_golds = set()
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a") as f:
        for i, q in enumerate(stream):
            g = q["expected_ids"][0]
            if learned and i not in done:
                props = bm25_top(q["text"], learned, df)
                qs = {}
                opts = {}
                for k, (_, d) in enumerate(props):
                    qs[f"a{k}"] = {"type": "noul", "instructions": {"past": d["text"][:800], "question": NOUL},
                                   "criteria": {"true": TRUE, "false": FALSE}}
                    opts[f"a{k}"] = d["text"][:800]
                opts["none"] = "都不是同一类情况"
                qs["best"] = {"type": "choice", "instructions": CHOICE, "criteria": opts}
                data = None
                for attempt in range(4):
                    data = await system_one(q["text"][:3000], qs, cfg, timeout=30)
                    if data:
                        break
                    await asyncio.sleep(3 * (attempt + 1))
                if data:
                    ans = data["answers"]
                    probs = ans["best"]["probabilities"]
                    rec = {"i": i, "familiar": g in seen_golds, "gold": g,
                           "props": [{"gold": d["gold"], "bm25": round(s, 2), "p": float(ans[f"a{k}"]["noul"]),
                                      "q": float(probs.get(f"a{k}", 0.0))} for k, (s, d) in enumerate(props)],
                           "none": float(probs.get("none", 0.0))}
                    f.write(json.dumps(rec) + "\n")
                    f.flush()
            # 学习:与经验流一致——这里按 reveal 概率学会(不模拟命中带来的学习,偏保守)
            if rng.random() < reveal:
                toks = tokenize(q["text"])
                learned.append({"text": q["text"], "toks": toks, "gold": g})
                for w in set(toks):
                    df[w] += 1
            seen_golds.add(g)



def report(names: list[str]) -> None:
    """按"取单选最高项,单选 ≥ q 且逐条 ≥ p"统计触发数、精度、认回熟悉情境的比例。"""
    for name in names:
        rows = [json.loads(line) for line in open(RESULTS / f"analogy_{name}.jsonl")]
        fam = sum(r["familiar"] for r in rows)
        print(f"== {name}: items {len(rows)}, familiar {fam}")
        for pth, qth in ((0.0, 0.7), (0.8, 0.0), (0.6, 0.7), (0.5, 0.7), (0.6, 0.8)):
            fires = ok = fam_ok = 0
            for r in rows:
                top = max(r["props"], key=lambda p: (p["q"], p["p"]))
                if top["q"] >= qth and top["p"] >= pth and top["q"] > r["none"]:
                    fires += 1
                    if top["gold"] == r["gold"]:
                        ok += 1
                        fam_ok += r["familiar"]
            print(f"  choice>={qth} noul>={pth}: fires {fires}, precision {ok / max(1, fires):.3f}, "
                  f"familiar recovered {fam_ok / max(1, fam):.1%}")


if __name__ == "__main__":
    if sys.argv[1] == "report":
        report(sys.argv[2:])
    else:
        asyncio.run(main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]) if len(sys.argv) > 3 else 0.5))
