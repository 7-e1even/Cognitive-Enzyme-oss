"""hardbench:前沿模型也答不好的竞赛级 / 专家级难题,全部来自公开的人写数据,带标准答案。

用途:给自我改进(RSI)评测提供"模型自己不会"的题——模型本来就会的题,记忆和经验没有用武之地。

| 来源 | 领域 | 题数 | 答案 | 判分 |
|---|---|---|---|---|
| BeyondAIME(字节 Seed,2025,CC0) | 奥数 | 100 | 整数 | 精确比对 |
| OlymMATH en-hard(人大,2025,MIT) | 奥数 | 100 | 数值 / 表达式 | 能算出数值就比数值,否则大模型判等价 |
| IMO-AnswerBench(DeepMind IMO-Bench,2025,CC-BY-4.0) | 奥数 | 400 | 短答案 | 同上 |
| AMO-Bench(美团,2025,MIT) | 奥数(超 IMO 难度) | 50 | 数值 / 集合 / 表达式 / 描述 | 同上,带官方解答 |
| PhysElite(2026-04) | 物理竞赛 | 英文、无图、难度 5–7 共 372 | 符号表达式,常有多小问 | 大模型判等价,带官方解答 |
| MedXpertQA Text(清华,2025,MIT) | 医学专家级 | 2450 | 十选一 | 精确比对 |
| FrontierScience 公开集(OpenAI,2026,Apache-2.0) | 物理 / 化学 / 生物 | 160 | 表达式 / 数值 | 大模型判等价 |

PhysElite 原始文本里有转义损坏(\\frac 变成换页符 + "rac",\\text 变成制表符 + "ext" 等),这里还原成反斜杠;
整包 944MB(图片),只通过 Hugging Face 的行接口取文字字段。

  .venv-eval/bin/python -m tests.eval.hardbench.build          # → tests/eval/hardbench/data/problems.json
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import Counter
from pathlib import Path

from tests.eval.realbench.build import CACHE, hf_rows

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PHYSELITE_ROWS = "https://datasets-server.huggingface.co/rows?dataset=physelite/PhysElite&config=default&split=default"

# 转义损坏:JSON / Python 把 LaTeX 的 \a \b \f \r \t \v 当成了控制字符
_CTRL = {"\x07": "\\a", "\x08": "\\b", "\x0b": "\\v", "\x0c": "\\f", "\r": "\\r", "\t": "\\t"}
_NL_CMD = re.compile(r"\n(?=abla|eq\b|eq[^a-z]|u[_^{}\\$ ,)]|ot\b|ewline|i\b)")


def repair(text: str) -> str:
    text = "".join(_CTRL.get(ch, ch) for ch in text or "")
    return _NL_CMD.sub(r"\\n", text)


def strip_boxed(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"\\boxed\s*\{", s)
    if not m:
        return s
    depth, start = 0, m.end() - 1
    for j in range(start, len(s)):
        depth += {"{": 1, "}": -1}.get(s[j], 0)
        if depth == 0:
            return s[start + 1:j].strip()
    return s


def _physelite_rows() -> list[dict]:
    cache = CACHE / "physelite_text.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    keep = ["problem_id", "language", "problem_text", "problem_solution", "problem_answer", "difficulty",
            "problem_tags"]
    out, offset, total = [], 0, None
    while total is None or offset < total:
        url = f"{PHYSELITE_ROWS}&offset={offset}&length=100"
        for attempt in range(6):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ce-eval"}),
                                            timeout=60) as r:
                    d = json.load(r)
                break
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(3 * (attempt + 1))
        total = d["num_rows_total"]
        for row in d["rows"]:
            r = row["row"]
            out.append({**{k: r.get(k) for k in keep}, "n_images": len(r.get("images") or [])})
        offset += 100
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def build() -> list[dict]:
    problems: list[dict] = []

    def add(**p):
        problems.append({"solution": "", "tags": [], "difficulty": "", **p})

    for i, r in enumerate(hf_rows("ByteDance-Seed/BeyondAIME", "default", "test")):
        add(id=f"beyondaime-{i:03d}", set="beyondaime", domain="math", problem=r["problem"],
            answer=str(r["answer"]).strip(), kind="integer")
    for r in hf_rows("RUC-AIBOX/OlymMATH", "en-hard", "test"):
        add(id=r["unique_id"], set="olymmath", domain="math", problem=r["problem"], answer=str(r["answer"]).strip(),
            kind="expression", tags=[r["subject"]])
    for r in hf_rows("Hwilner/imo-answerbench", "default", "train"):
        add(id=r["Problem ID"], set="imo_answer", domain="math", problem=r["Problem"],
            answer=str(r["Short Answer"]).strip(), kind="expression", tags=[r["Category"], r["Subcategory"]],
            source=r["Source"])
    for r in hf_rows("meituan-longcat/AMO-Bench", "default", "test"):
        add(id=f"amo-{r['question_id']}", set="amo", domain="math", problem=r["prompt"],
            answer=strip_boxed(r["answer"]), kind="expression" if r["answer_type"] == "number" else "free",
            solution=r["solution"], tags=[r["answer_type"]])
    for r in _physelite_rows():
        if r["language"] != "en" or r["n_images"] or str(r["difficulty"]) not in ("5", "6", "7"):
            continue
        add(id=f"physelite-{r['problem_id']}", set="physelite", domain="physics", problem=repair(r["problem_text"]),
            answer=repair(r["problem_answer"]), kind="free", solution=repair(r["problem_solution"]),
            tags=list(r["problem_tags"] or []), difficulty=str(r["difficulty"]))
    for r in hf_rows("TsinghuaC3I/MedXpertQA", "Text", "test"):
        add(id=f"medxpert-{r['id']}", set="medxpert", domain="medicine", problem=r["question"], answer=r["label"],
            kind="choice", tags=[r["medical_task"], r["body_system"], r["question_type"]])
    for i, r in enumerate(hf_rows("openai/frontierscience", "default", "test")):
        clean = lambda s: (s or "").replace("`", "").strip().rstrip("'").strip()     # noqa: E731
        add(id=f"frontiersci-{i:03d}", set="frontiersci", domain=r["subject"], problem=clean(r["problem"]),
            answer=clean(r["answer"]), kind="free", tags=[r["subject"]])
    return problems


def main() -> None:
    problems = build()
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "problems.json").write_text(json.dumps(problems, ensure_ascii=False, indent=0), encoding="utf-8")
    counts = Counter((p["set"], p["kind"]) for p in problems)
    for (s, k), n in sorted(counts.items()):
        print(f"{s:<12} {k:<10} {n}")
    print(f"共 {len(problems)} 题 → {DATA / 'problems.json'}")


if __name__ == "__main__":
    main()
