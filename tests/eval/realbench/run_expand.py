"""查询扩写(HyDE 式)对首次召回的影响 —— 离线实验。

静态真实基准里 35% 的 miss 是"根本没召回"(安全 68%、法律 57%):记忆写的是抽象知识,
情境是具体描述。检索前先让 LLM 把情境抽象成"哪一类已存知识最有用",再拿这段描述去检索,
与原查询的召回按酶取最高分合并。评测数据仍全部是真实数据——扩写只是检索时的推理步骤。

两步(第二步用本地模型服务,务必在评测队列空闲时跑):
  GLM_API_KEY=... python3 tests/eval/realbench/run_expand.py generate
  (real_env) .venv-eval/bin/python -m tests.eval.realbench.run_expand measure
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
CACHE = DATA / "expansions.json"
GLM_URL = os.environ.get("GLM_API_URL", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5")

# 与领域无关:线上系统事先不知道情境属于哪个领域
PROMPT = ("You are helping a memory system recall relevant stored knowledge. Read the situation below and write "
          "a short description (2-4 sentences) of the kind of stored knowledge that would be most useful for it: "
          "the underlying principle, rule, known issue, weakness type, or research finding, stated generally, the "
          "way a knowledge-base entry would state it. Do not solve the problem. Output only the description.\n\n"
          "Situation:\n{text}")


def _glm(text: str, key: str) -> str:
    body = json.dumps({"model": GLM_MODEL, "temperature": 0, "max_tokens": 300,
                       "thinking": {"type": "disabled"},
                       "messages": [{"role": "user", "content": PROMPT.format(text=text[:3000])}]}).encode()
    for attempt in range(6):
        try:
            req = urllib.request.Request(GLM_URL, data=body, headers={
                "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return (json.loads(resp.read())["choices"][0]["message"].get("content") or "").strip()
        except Exception:
            time.sleep(min(3 * 2 ** attempt, 60))
    return ""


def generate() -> None:
    key = os.environ["GLM_API_KEY"]
    queries = [q for q in json.loads((DATA / "queries.json").read_text(encoding="utf-8"))["queries"]
               if q["kind"] == "positive"]
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [q for q in queries if not cache.get(q["id"])]
    with ThreadPoolExecutor(3) as ex:
        for q, out in zip(todo, ex.map(lambda q: _glm(q["text"], key), todo)):
            cache[q["id"]] = out
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"expansions: {sum(bool(v) for v in cache.values())}/{len(queries)}")


async def measure() -> None:
    from app.core.features import build_search_query
    from app.llm_client import embed
    from tests.eval.bootstrap import eval_stores

    config, pg, qdrant = await eval_stores()
    queries = [q for q in json.loads((DATA / "queries.json").read_text(encoding="utf-8"))["queries"]
               if q["kind"] == "positive"]
    exp = json.loads(CACHE.read_text(encoding="utf-8"))
    ranks = defaultdict(lambda: defaultdict(list))
    for q in queries:
        vo = await embed(build_search_query(q["text"], None), config=config.embedding)
        ve = await embed(exp.get(q["id"], "") or q["text"], config=config.embedding)
        ho = {h["id"]: h["_score"] for h in await qdrant.search(vo, limit=200)}
        he = {h["id"]: h["_score"] for h in await qdrant.search(ve, limit=200)}
        merged = {k: max(ho.get(k, -1), he.get(k, -1)) for k in set(ho) | set(he)}
        # RRF:只看各路名次,不受两路相似度尺度不同的影响(扩写文本与记忆同为"知识腔调",分数系统性偏高)
        ro = {k: i for i, k in enumerate(sorted(ho, key=ho.get, reverse=True))}
        re_ = {k: i for i, k in enumerate(sorted(he, key=he.get, reverse=True))}
        rrf = {k: (1 / (60 + ro[k]) if k in ro else 0) + (1 / (60 + re_[k]) if k in re_ else 0)
               for k in set(ho) | set(he)}
        for name, sc in (("orig", ho), ("expand", he), ("merge", merged), ("rrf", rrf)):
            order = sorted(sc, key=sc.get, reverse=True)
            for g in q["expected_ids"]:
                ranks[q["domain"]][name].append(order.index(g) + 1 if g in order else 999)
    out = {}
    for dom, by in sorted(ranks.items()):
        out[dom] = {name: {f"R@{k}": round(sum(r <= k for r in rs) / len(rs), 3) for k in (12, 36, 50)}
                    for name, rs in by.items()}
        print(dom, json.dumps(out[dom]))
    (DATA / "results").mkdir(parents=True, exist_ok=True)
    (DATA / "results" / "expand_recall.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    await pg.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["generate"]:
        generate()
    else:
        import asyncio
        asyncio.run(measure())
