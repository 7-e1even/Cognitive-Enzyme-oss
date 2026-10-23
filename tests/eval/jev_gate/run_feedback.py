"""被动评估判卷准确度:LLM(capture 现行提示词,逐字复刻)vs Jev(三选一 choice)vs 组合。

判错的代价不对称:把 neutral 判成 helped/contradicted = 学分记错(污染学习),
把 helped/contradicted 判成 neutral = 只是少学一次。所以主指标是"记错率"。

  TYPESAFE_API_KEY=... GLM_API_KEY=... python3 tests/eval/jev_gate/run_feedback.py
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_jev_gate import EVAL, HERE, MODEL, OUT, _load_cache, call_jev  # noqa: E402

GLM_URL = os.environ.get("GLM_API_URL", "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-5")
VERDICTS = ("helped", "contradicted", "neutral")

# ── capture.evaluate_injections 的提示词(逐字复刻) ──
_SYS = "你负责冷静评估记忆注入的实际效果,只依据对话中的证据下结论。只返回 JSON 数组。"
_JSON_SUFFIX = "\n\n回复必须是合法的 JSON，不要包含 markdown 代码块标记。"   # llm_client.chat_json 追加


def capture_prompt(rules: list[dict], transcript: str) -> str:
    block = "\n".join(f"- id={r['id']} | 建议: {r['then']} | 依据: {r['because']}" for r in rules)
    return f"""以下「直觉规则」曾在本次 AI 编程会话中被自动注入(推送给 agent 参考)。
请根据对话记录逐条评估其实际效果。

规则清单:
{block}

对话记录:
\"\"\"
{transcript[:12000]}
\"\"\"

判定标准(必须能在对话记录中指认证据;证据不足一律 neutral,不要臆断):
- "helped"        建议被明确采纳/遵循,且看得出正向效果或用户认可
- "contradicted"  建议被用户否定、被证明是错的、或照做后出了问题
- "neutral"       没被采纳、看不出影响、或证据不足

只返回 JSON 数组,每条规则一个元素:
[{{"id": "规则id", "verdict": "helped|contradicted|neutral", "evidence": "对话中的一句原文证据(neutral 可为空)"}}]"""


# ── Jev:每条规则一道三选一 ──
JEV_Q = "在这段对话记录(state)中,`rule` 这条被注入的建议的实际效果是?"
JEV_CRIT = {
    "helped": "建议被明确采纳/遵循,且看得出正向效果或用户认可",
    "contradicted": "建议被用户否定、被证明是错的、或照做后出了问题",
    "neutral": "建议没被采纳、与对话内容无关、看不出影响、或证据不足",
}


def glm_call(payload: dict, cache: dict, key: str) -> dict:
    ck = "glm:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if ck in cache:
        return cache[ck]
    body = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(6):
        req = urllib.request.Request(GLM_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            row = {"key": ck, "latency": time.perf_counter() - t0, "response": data}
            cache[ck] = row
            with (OUT / "cache.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row
        except Exception:
            time.sleep(min(3 * 2 ** attempt, 60))
    raise RuntimeError("GLM call failed")


def llm_verdicts(case, rules, cache, key) -> dict[str, str]:
    payload = {"model": GLM_MODEL, "temperature": 0.5, "max_tokens": 1024,
               "thinking": {"type": "disabled"},
               "messages": [{"role": "system", "content": _SYS + _JSON_SUFFIX},
                            {"role": "user", "content": capture_prompt(rules, case["transcript"])}]}
    text = glm_call(payload, cache, key)["response"]["choices"][0]["message"].get("content") or ""
    m = re.search(r"\[.*\]", text, re.S)
    out = {r["id"]: "neutral" for r in rules}
    try:
        for item in json.loads(m.group(0)) if m else []:
            v = (item.get("verdict") or "neutral").strip().lower()
            if item.get("id") in out and v in VERDICTS:
                out[item["id"]] = v
    except (json.JSONDecodeError, AttributeError):
        pass
    return out


def jev_verdicts(case, rules, cache, key) -> dict[str, dict]:
    qs = {f"r{i}": {"type": "choice",
                    "instructions": {"rule": {"建议": r["then"], "依据": r["because"]}, "question": JEV_Q},
                    "criteria": JEV_CRIT}
          for i, r in enumerate(rules)}
    ans = call_jev({"model": MODEL, "state": case["transcript"], "questions": qs}, cache, key)["response"]["answers"]
    return {r["id"]: ans[f"r{i}"] for i, r in enumerate(rules)}


def main() -> None:
    ts_key, glm_key = os.environ["TYPESAFE_API_KEY"], os.environ["GLM_API_KEY"]
    corpus = {e["id"]: e for f in glob.glob(str(EVAL / "corpus" / "*.json")) for e in json.load(open(f))}
    cases = json.load((HERE / "feedback_judge.json").open(encoding="utf-8"))["cases"]
    cache = _load_cache()

    def one(case):
        rules = [{"id": rid, "then": corpus[rid]["then"], "because": corpus[rid].get("because", "")}
                 for rid in case["rules"]]
        return case, llm_verdicts(case, rules, cache, glm_key), jev_verdicts(case, rules, cache, ts_key)

    with ThreadPoolExecutor(3) as ex:
        done = list(ex.map(one, cases))

    rows = []
    for case, llm, jev in done:
        for rid, truth in case["labels"].items():
            j = jev[rid]
            rows.append({"case": case["id"], "rule": rid, "truth": truth, "llm": llm[rid],
                         "jev": j["choice"], "jev_probs": j["probabilities"], "jev_conf": j["confidence"]})

    def strategies(r):
        llm, jev, pj = r["llm"], r["jev"], r["jev_probs"]
        return {
            "llm(现行)": llm,
            "jev": jev,
            "一致才记": llm if llm == jev else "neutral",
            "llm∧jev≥0.3": llm if llm == "neutral" or pj.get(llm, 0) >= 0.3 else "neutral",
            "jev 高置信": jev if r["jev_conf"] >= 0.6 else "neutral",
        }

    report = {}
    for name in strategies(rows[0]):
        preds = [(r["truth"], strategies(r)[name]) for r in rows]
        acc = sum(t == p for t, p in preds) / len(preds)
        wrong_credit = sum(p != "neutral" and p != t for t, p in preds)
        missed = sum(t != "neutral" and p == "neutral" for t, p in preds)
        opposite = sum({t, p} == {"helped", "contradicted"} for t, p in preds)
        report[name] = {"acc": round(acc, 3), "记错": wrong_credit, "漏记": missed, "方向反": opposite,
                        "confusion": dict(Counter(f"{t}->{p}" for t, p in preds if t != p))}
        print(f"{name:14s} acc={acc:.3f} 记错={wrong_credit} 漏记={missed} 方向反={opposite} {report[name]['confusion']}")
    (OUT / "feedback_results.json").write_text(json.dumps({"rows": rows, "report": report},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
