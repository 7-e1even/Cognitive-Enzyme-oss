"""hardbench 作答与判分:agent(pi + GLM-5.3)闭卷做题,程序判分;判不了的交给大模型判等价。

判分:
  integer     最后一行 Answer 里的整数与标准答案相同
  choice      选项字母相同
  expression  两边都能算出数值(分数、根式、π、幂、多个值的集合)就比数值(相对误差 1e-6);否则判等价
  free        大模型判等价:看题目、标准答案与作答的最终答案,多小问要全对;数值型物理量允许 1% 误差

判等价用同一个 agent(pi + GLM-5.3,思考 low),只看最终答案行,不看推理过程。

  .venv-eval/bin/python -m tests.eval.hardbench.run --tag pilot --per-set 10
  .venv-eval/bin/python -m tests.eval.hardbench.run --tag pilot --report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from tests.eval.realbench.uplift_eval import _eval, api_key, ask_with_retry, final_answer
from tests.eval.hardbench.grading import judge_grade, outcome

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "results"


# ── 数值化:LaTeX 答案 → 数值 ─────────────────────────────────────

def _match_brace(s: str, i: int) -> int:
    depth = 0
    for j in range(i, len(s)):
        depth += {"{": 1, "}": -1}.get(s[j], 0)
        if depth == 0:
            return j
    return -1


def _unbox(s: str) -> str:
    m = re.search(r"\\boxed\s*\{", s)
    if not m:
        return s
    end = _match_brace(s, m.end() - 1)
    return s[m.end():end] if end > 0 else s


def latex_to_expr(s: str) -> str:
    s = _unbox((s or "").strip())
    s = s.replace("$", " ").replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    s = re.sub(r"\\(?:left|right|displaystyle|,|;|!|quad|qquad)|\\ ", " ", s)
    s = re.sub(r"\\(?:text|mathrm|operatorname)\s*\{[^{}]*\}", " ", s)
    while True:                                       # 由外向内展开 \frac{a}{b}、\sqrt[n]{x}、\sqrt{x}
        m = re.search(r"\\frac\s*\{", s)
        if m:
            a1 = _match_brace(s, m.end() - 1)
            if a1 > 0 and s[a1 + 1:a1 + 2] == "{":
                b1 = _match_brace(s, a1 + 1)
                if b1 > 0:
                    s = f"{s[:m.start()]}(({s[m.end():a1]})/({s[a1 + 2:b1]})){s[b1 + 1:]}"
                    continue
        m = re.search(r"\\sqrt\s*\[([^\]]+)\]\s*\{", s)
        if m:
            x1 = _match_brace(s, m.end() - 1)
            if x1 > 0:
                s = f"{s[:m.start()]}(({s[m.end():x1]})**(1/({m.group(1)}))){s[x1 + 1:]}"
                continue
        m = re.search(r"\\sqrt\s*\{", s)
        if m:
            x1 = _match_brace(s, m.end() - 1)
            if x1 > 0:
                s = f"{s[:m.start()]}sqrt({s[m.end():x1]}){s[x1 + 1:]}"
                continue
        break
    s = re.sub(r"\\sqrt\s*(\d+)", r"sqrt(\1)", s)
    s = s.replace("\\cdot", "*").replace("\\times", "*").replace("×", "*").replace("\\pi", "pi").replace("π", "pi")
    s = s.replace("\\ln", "ln").replace("\\log", "log").replace("\\exp", "exp").replace("\\{", "").replace("\\}", "")
    s = s.replace("^", "**").replace("{", "(").replace("}", ")").replace("−", "-")
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)     # 千分位
    s = re.sub(r"(\d|\))\s*(?=(?![eE][-+]?\d)[a-z(])", r"\1*", s)   # 隐式乘法:2pi、2(3)、)(
    return s.strip().rstrip(".").strip()


def _split_top(s: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch in ",;" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return [p.strip() for p in parts + [cur] if p.strip()]


def latex_values(s: str) -> list[float] | None:
    """能完全算成数值(或若干数值)就返回,否则 None。"""
    expr = latex_to_expr(s)
    if "=" in expr:
        expr = expr.split("=")[-1]
    if not expr or re.search(r"[A-Za-z]", re.sub(r"\b(?:pi|sqrt|ln|log|exp|e)\b", "", expr)):
        return None                                   # 还剩变量(x、n、f……):不是纯数值
    out = []
    for part in _split_top(expr):
        try:
            v = _eval(__import__("ast").parse(part, mode="eval"))
        except Exception:
            return None
        if not isinstance(v, float) or v != v:
            return None
        out.append(v)
    return out or None


def same_values(a: list[float], b: list[float]) -> bool:
    if len(a) != len(b):
        return False
    close = lambda x, y: abs(x - y) <= 1e-6 * max(1.0, abs(y))    # noqa: E731
    return all(close(x, y) for x, y in zip(sorted(a), sorted(b)))


# ── 判分 ───────────────────────────────────────────────────────

JUDGE_PROMPT = """You are grading a final answer to a {domain} problem against the official reference answer.

Problem:
{problem}

Official reference answer:
{answer}

Candidate's final answer:
{pred}

Is the candidate's final answer equivalent to the reference answer for the parts actually requested in the problem? Count algebraically equivalent expressions, equivalent units and notation, and numerically equal values (within 1% for numeric physical quantities) as equivalent. Match every requested part covered by the reference, but ignore extra reference parts that the problem does not ask for. Do not solve the problem again; only compare the answers.
Reply with exactly one word on the last line: EQUIVALENT or NOT_EQUIVALENT"""


_FINAL_HEAD = re.compile(r"(?im)^[\s>*_#`]*(?:final\s+answer|answer)[\s*_`:：]*$")


def extract_final(text: str) -> str | None:
    """最终答案:Answer: 行;单独一行的 FINAL ANSWER / Answer 标题后面那一段;最后一个 \\boxed{}。"""
    raw = final_answer(text)
    if raw and raw.strip("*_` "):
        return raw
    heads = list(_FINAL_HEAD.finditer(text or ""))
    if heads:
        block = text[heads[-1].end():].strip()
        if block:
            return block
    m = list(re.finditer(r"\\boxed\s*\{", text or ""))
    if m:
        end = _match_brace(text, m[-1].end() - 1)
        if end > 0:
            return text[m[-1].end():end]
    return None


def grade_local(p: dict, text: str) -> dict:
    """确定性判分;需要大模型判等价时返回 need_judge。"""
    raw = extract_final(text)
    if raw is None:
        return {"pred": None, "correct": False, "parsed": False, "graded_by": "none"}
    if p["kind"] == "choice":
        m = re.search(r"\b([A-J])\b", raw.replace("(", " ").replace(")", " "))
        pred = m.group(1) if m else None
        return {"pred": pred, "correct": pred == p["answer"], "parsed": pred is not None, "graded_by": "exact"}
    if p["kind"] == "integer":
        m = re.findall(r"-?\d+", raw.replace(",", ""))
        pred = m[-1] if m else None
        return {"pred": pred, "correct": pred is not None and int(pred) == int(p["answer"]), "parsed": pred is not None,
                "graded_by": "exact"}
    if p["kind"] == "expression":
        ref, got = latex_values(p["answer"]), latex_values(raw)
        if ref is not None and got is not None:
            return {"pred": raw[:200], "correct": same_values(got, ref), "parsed": True, "graded_by": "numeric"}
    return {"pred": raw, "need_judge": True, "parsed": True}


def parse_verdict(text: str) -> bool | None:
    lines = [line.strip(" *_`\t\r.") for line in (text or "").splitlines() if line.strip()]
    tail = lines[-1].upper() if lines else ""
    if tail in ("NOT_EQUIVALENT", "NOT EQUIVALENT"):
        return False
    if tail == "EQUIVALENT":
        return True
    return None


# ── 主流程 ─────────────────────────────────────────────────────

def solve_prompt(p: dict) -> str:
    if p["kind"] == "choice":
        return (f"{p['problem']}\n\nChoose the single best answer. End your reply with a final line in exactly this "
                "form:\nAnswer: <letter>")
    tail = ("If the problem has several parts, give all final results on that line, separated by semicolons."
            if p["domain"] != "math" else "")
    return (f"{p['problem']}\n\nSolve the problem. End your reply with a final line in exactly this form:\n"
            f"Answer: <final answer>\n{tail}").rstrip()


def load_problems(sets: list[str], per_set: int, seed: int) -> list[dict]:
    allp = json.loads((DATA / "problems.json").read_text(encoding="utf-8"))
    by_set: dict[str, list[dict]] = defaultdict(list)
    for p in allp:
        if p["set"] in sets:
            by_set[p["set"]].append(p)
    rng = random.Random(seed)
    chosen = {s: (rng.sample(v, per_set) if per_set and per_set < len(v) else v) for s, v in by_set.items()}
    out, i = [], 0                                    # 各集合轮流出题
    while any(i < len(v) for v in chosen.values()):
        out += [chosen[s][i] for s in sets if s in chosen and i < len(chosen[s])]
        i += 1
    return out


def load_rows(path: Path) -> dict[str, dict]:
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[f"{r['id']}|{r['arm']}"] = r
    return rows


async def run(args) -> None:
    sets = [s for s in args.sets.split(",") if s]
    problems = load_problems(sets, args.per_set, args.seed)
    OUT.mkdir(parents=True, exist_ok=True)
    path, log_path = OUT / f"{args.tag}.jsonl", OUT / f"{args.tag}.log"
    done = {k for k, r in load_rows(path).items() if not r.get("error")}

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    env = dict(os.environ, PI_OFFLINE="1", PI_TELEMETRY="0")
    if args.key_from:
        env[args.key_env] = api_key(args.key_from)
    cwd = OUT / "pi_cwd"
    cwd.mkdir(exist_ok=True)
    solver = SimpleNamespace(model=args.model, thinking=args.thinking, timeout=args.timeout, max_wait=args.max_wait)
    judge = SimpleNamespace(model=args.model, thinking=args.judge_thinking, timeout=900.0, max_wait=args.max_wait)
    todo = [p for p in problems if f"{p['id']}|none" not in done]
    log(f"tag={args.tag} model={args.model} thinking={args.thinking} 题目 {len(problems)} 待跑 {len(todo)} · "
        + ", ".join(f"{s} {sum(p['set'] == s for p in problems)}" for s in sets))
    sem, lock, n = asyncio.Semaphore(args.concurrency), asyncio.Lock(), {"done": 0}

    async def one(p: dict) -> None:
        async with sem:                               # 作答与判等价连续占同一个名额(见 rsi.py solve)
            res = await ask_with_retry(solve_prompt(p), solver, env, cwd, log)
            row = {"id": p["id"], "set": p["set"], "domain": p["domain"], "kind": p["kind"], "arm": "none",
                   "secs": res.get("secs"), "retries": res.get("retries", 0), "usage": res.get("usage"),
                   "thinking_chars": res.get("thinking_chars"), "stop": res.get("stop"),
                   "text": (res.get("text") or "")[-6000:]}
            if not res["ok"]:
                row["error"] = res.get("error", "unknown")
            else:
                g = grade_local(p, res.get("text") or "")
                if g.pop("need_judge", False):
                    jr = await ask_with_retry(JUDGE_PROMPT.format(domain=p["domain"], problem=p["problem"],
                                                                  answer=p["answer"], pred=g["pred"]),
                                              judge, env, cwd, log)
                    verdict = parse_verdict(jr.get("text") or "") if jr["ok"] else None
                    g.update(judge_grade(verdict, jr, model=judge.model))
                row.update(g)
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n["done"] += 1
            mark = "错误" if row.get("error") else ("对" if row.get("correct") else "错")
            log(f"  [{n['done']}/{len(todo)}] {p['set']:<11} {p['id']:<28} {mark} {row.get('graded_by', '')} "
                f"{row.get('secs')}s out={(row.get('usage') or {}).get('output')} pred={str(row.get('pred'))[:50]}"
                + (f" err={row['error'][:120]}" if row.get("error") else ""))

    await asyncio.gather(*(one(p) for p in todo))
    report(args.tag)


def report(tag: str) -> str:
    rows = [r for r in load_rows(OUT / f"{tag}.jsonl").values() if r["arm"] == "none"]
    by = defaultdict(list)
    for r in rows:
        by[r["set"]].append(r)
    lines = [f"# hardbench 闭卷正确率:{tag}", "",
             "判分失败不计为答错,单列未判定;正确率分母为有效判分。", "",
             "| 集合 | 有效判分 | 答对 | 正确率 | 判分方式 | 解析失败 | 未判定 | 作答错误 | 耗时中位数 | 输出 token 中位数 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for s, rs in sorted(by.items()):
        ok = [r for r in rs if outcome(r) is not None]
        correct = sum(bool(r.get("correct")) for r in ok)
        how = defaultdict(int)
        for r in ok:
            how[r.get("graded_by", "?")] += 1
        secs = sorted(r["secs"] for r in ok if r.get("secs"))
        outs = sorted((r.get("usage") or {}).get("output") or 0 for r in ok)
        ungraded = sum(not r.get("error") and outcome(r) is None for r in rs)
        errors = sum(bool(r.get("error")) for r in rs)
        lines.append(f"| {s} | {len(ok)} | {correct} | {correct / len(ok):.0%} | "
                     f"{', '.join(f'{k} {v}' for k, v in sorted(how.items()))} | {sum(not r.get('parsed') for r in ok)} | "
                     f"{ungraded} | {errors} | {secs[len(secs) // 2] if secs else '—'}s | {outs[len(outs) // 2] if outs else '—'} |" if ok
                     else f"| {s} | 0 | 0 | — | — | — | {ungraded} | {errors} | — | — |")
    text = "\n".join(lines)
    (OUT / f"{tag}.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--sets", default="beyondaime,olymmath,imo_answer,amo,physelite,medxpert,frontiersci")
    ap.add_argument("--per-set", type=int, default=0, help="每个集合抽几道(0 = 全部)")
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--model", default="zai-coding-cn/glm-5.3")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--judge-thinking", default="low")
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
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
