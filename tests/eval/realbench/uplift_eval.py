"""注入的真实效果:同一个 agent 在自带标准答案的真实任务上,分别在三种条件下作答,程序判分。

  none    不注入
  system  注入系统实际选中的记忆:直接用 API 评测台现默认配置记下的注入结果
          (tests/eval/reports/real_api_lex.outcomes.jsonl),不重跑检索;没注入的题与 none 条件相同,复用 none 的回答
  gold    只注入标准答案对应的记忆

回答三个问题:正确的记忆本身能不能让强模型答得更好(gold − none);系统实际拿到了其中多少(system − none);
推错的记忆会不会把模型带偏(system 组里注入的全是错记忆的题,以及答案是不是正好取自某条错记忆)。

任务(realbench 的真实数据,判分不用大模型)。默认只跑模型本来不会的三个专业领域——模型自己就会的题,
记忆没有用武之地,也测不出自我改进:
  firefox   2026 年用户提交的 Firefox 缺陷报告 → 它重复的是哪个已知 bug(bug 号);允许答 NEW
  legal     联邦判决里抹掉引注的上下文 → 这里引用的是哪个先例(案名,按双方当事人名匹配);允许答 UNKNOWN。
            去掉引文以 [ 开头的 16 条(改写过的转引,原始出处有歧义);同一段原文出现在几个判例里时,答哪个都算对
  security  NVD 的 CVE 描述 → CWE 编号;严格 = NVD 标注,宽松 = 再算上 CWE 官方的父子条目
另有两个领域可以用 --domains 选,但 GLM-5.3 不注入就答对 94–95%,只作对照:
  theorem   TheoremQA 原题 → 数值 / 是非 / 列表 / 选项;按 TheoremQA 官方口径,数值允许 4% 相对误差
  biomed    SciFact 论断 → 支持 / 反驳;只取对应论文带证据标签的 95 条(官方 dev 集,另外 55 条是"证据不足")

注入块与 hooks/ce_prime_hook.py 的渲染一字不差,放在题目前面。记忆 id 换成不透明的短哈希:线上 id 是 UUID,
而评测库的 id(如 cwe-79)会直接泄露答案。判例记忆的"因为"一栏换成完整引注(案名、卷页、年份),和真实的
判例记忆一样;评测库为了逼检索按语义匹配没放案名,检索结果取自日志,不受影响。

agent:pi(非交互 JSON 模式),不给工具(防止上网查答案),不加载扩展、技能、提示模板和上下文文件,不存会话,
在空目录里跑。默认模型 zai-coding-cn/glm-5.3、思考 high;密钥取自 pi 里已配置的服务商,只放进子进程的环境变量。

稳定性:同一道题的三种条件连着跑,中途停下也是配对数据;领域轮流出题;逐条落盘,--resume 续跑(出错的行会重跑);
限流 / 额度用尽时退避等待,其他错误重试 3 次;每条记录耗时、token、思考长度、重试次数与错误。

  .venv-eval/bin/python -m tests.eval.realbench.uplift_eval --tag glm53 [--limit 5] [--resume]
  .venv-eval/bin/python -m tests.eval.realbench.uplift_eval --tag glm53 --report     # 只出报表
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import fcntl
import functools
import hashlib
import json
import math
import operator
import os
import random
import re
import subprocess
import shutil
import tarfile
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATA = HERE / "data"
OUT = DATA / "results"
CACHE = Path.home() / ".cache" / "ce-realbench"
OUTCOMES = ROOT / "tests" / "eval" / "reports" / "real_api_lex.outcomes.jsonl"
SCIFACT_URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
DOMAINS = ("firefox", "legal", "security", "theorem", "biomed")
DEFAULT_DOMAINS = ("firefox", "legal", "security")
LEPARD = CACHE / "rmahari__LePaRD__default__train__0.parquet"      # build 用的同一个分片

SEV_ICON = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
RATE_LIMIT = re.compile(r"429|rate.?limit|too many|quota|usage limit|1302|1308|1313|上限|频率|余额|限流", re.I)


# ── 任务 ───────────────────────────────────────────────────────

def _scifact_labels() -> dict[str, dict[str, str]]:
    """SciFact 官方 dev 集:claim id → {论文 id: SUPPORT / CONTRADICT}。"""
    from tests.eval.realbench.build import fetch
    path = CACHE / "scifact_data.tar.gz"
    if not path.exists():
        fetch(SCIFACT_URL, path.name)
    out: dict[str, dict[str, str]] = {}
    with tarfile.open(path) as tf:
        for line in tf.extractfile("data/claims_dev.jsonl").read().decode().splitlines():
            c = json.loads(line)
            out[str(c["id"])] = {doc: ev[0]["label"] for doc, ev in (c.get("evidence") or {}).items() if ev}
    return out


@functools.lru_cache(maxsize=1)
def _lepard() -> tuple[dict[str, dict], dict[str, frozenset[str]]]:
    """LePaRD:段落 id → 出处判例;同一段原文(前 80 字)出现过的所有出处案名。"""
    import pyarrow.parquet as pq
    rows = pq.read_table(LEPARD, columns=["passage_id", "source_name", "source_cite", "quote"]).to_pylist()
    by_pid: dict[str, dict] = {}
    by_quote: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        by_pid.setdefault(r["passage_id"], r)
        by_quote[(r["quote"] or "")[:80]].add(r["source_name"])
    return by_pid, {k: frozenset(v) for k, v in by_quote.items()}


THEOREM_HINT = {
    "integer": "an integer",
    "float": "a number",
    "bool": "True or False",
    "list of integer": "a list of integers, e.g. [1, 2, 3]",
    "list of float": "a list of numbers, e.g. [0.5, 1.2]",
    "option": "the option letter, e.g. (a)",
}


def load_tasks(domains: tuple[str, ...]) -> list[dict]:
    queries = json.loads((DATA / "queries.json").read_text(encoding="utf-8"))["queries"]
    tqa = {q["id"]: q for q in json.loads((CACHE / "theoremqa_test.json").read_text(encoding="utf-8"))}
    labels = _scifact_labels() if "biomed" in domains else {}
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for q in queries:
        d = q.get("domain")
        if d not in domains:
            continue
        task = {"id": q["id"], "domain": d, "gold_ids": list(q["expected_ids"])}
        if d == "firefox":
            task.update(answer=[g.removeprefix("bz-") for g in q["expected_ids"]], prompt=(
                f"A Firefox user filed this bug report:\n\n{q['text']}\n\n"
                "Is this a duplicate of an existing, already-reported Mozilla bug? If you know which bug it "
                "duplicates, give that bug's number. If you do not know of one, answer NEW.\n"
                "End your reply with a final line in exactly this form:\n"
                "Answer: <bug number>   or   Answer: NEW"))
        elif d == "legal":
            by_pid, by_quote = _lepard()
            src = by_pid.get(q["expected_ids"][0].removeprefix("lep-"))
            if not src or (src["quote"] or "").lstrip().startswith("["):
                continue
            task.update(answer=src["source_name"], acceptable=sorted(by_quote[(src["quote"] or "")[:80]]), prompt=(
                "Below is an excerpt from a U.S. court opinion. Case citations in it have been replaced with "
                "[citation]. The excerpt ends where the author quotes or cites an earlier court opinion (that "
                f"citation has been removed).\n\n{q['text']}\n\n"
                "Which earlier court opinion is being quoted or cited at the end of the excerpt? Give that case's "
                "name. If you cannot identify it, answer UNKNOWN.\n"
                "End your reply with a final line in exactly this form:\n"
                "Answer: <case name>   or   Answer: UNKNOWN"))
        elif d == "theorem":
            t = tqa[q["source"].removeprefix("TheoremQA:")]
            task.update(answer=t["Answer"], answer_type=t["Answer_type"], prompt=(
                f"{q['text']}\n\nSolve this problem. End your reply with a final line in exactly this form:\n"
                f"Answer: <{THEOREM_HINT[t['Answer_type']]}>"))
        elif d == "security":
            task.update(answer=list(q["expected_ids"]), acceptable=list(q.get("acceptable_ids") or []), prompt=(
                f"The following is a vulnerability description from the NVD:\n\n{q['text']}\n\n"
                "Which CWE (Common Weakness Enumeration) entry best describes the underlying weakness? "
                "Pick the single most specific CWE.\n"
                "End your reply with a final line in exactly this form:\nAnswer: CWE-<number>"))
        else:
            ev = labels.get(q["id"].removeprefix("sf-q"), {})
            labs = {ev[g.removeprefix("sf-")] for g in q["expected_ids"] if g.removeprefix("sf-") in ev}
            if len(labs) != 1:          # 证据不足(NEI)或标签冲突:没有唯一的标准答案
                continue
            task.update(answer=labs.pop(), prompt=(
                f"Scientific claim: {q['text']}\n\n"
                "According to the published biomedical research literature, is this claim supported or "
                "contradicted by the evidence? If you are unsure, give your best judgment.\n"
                "End your reply with a final line in exactly this form:\n"
                "Answer: SUPPORT   or   Answer: CONTRADICT"))
        by_domain[d].append(task)
    # 领域轮流出题:中途停下时各领域进度相当
    out, i = [], 0
    while any(i < len(v) for v in by_domain.values()):
        out += [by_domain[d][i] for d in domains if i < len(by_domain[d])]
        i += 1
    return out


def load_corpus(domains: tuple[str, ...]) -> dict[str, dict]:
    corpus: dict[str, dict] = {}
    for d in domains:
        for e in json.loads((DATA / "corpus" / f"{d}.json").read_text(encoding="utf-8")):
            if d == "legal":            # 判例记忆带上完整引注,和真实的判例记忆一样
                src = _lepard()[0].get(e["id"].removeprefix("lep-"))
                if src:
                    e = {**e, "because": src["source_cite"], "case": src["source_name"]}
            corpus[e["id"]] = e
    return corpus


def load_system_injections() -> dict[str, list[str]]:
    rows = [json.loads(line) for line in OUTCOMES.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["query_id"]: list(r.get("injected") or []) for r in rows}


# ── 注入块(与 hooks/ce_prime_hook.py 一字不差)──────────────────────

def opaque_id(memory_id: str) -> str:
    return hashlib.sha1(memory_id.encode()).hexdigest()[:12]


def render_block(memories: list[dict]) -> str:
    lines = ["⟦CE:BEGIN⟧",
             "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）："]
    for m in memories:
        conf = float(m.get("confidence", 0.5))
        icon, tag = SEV_ICON.get(m.get("severity", "info"), "⚪"), f"[{conf:.0%}]"
        line = f"{icon} {tag} {m.get('then', '')}"
        if m.get("because"):
            line += f"（因为：{m['because']}）"
        line += f"  ‹enzyme_id={opaque_id(m['id'])}›"
        lines.append(line)
    lines.append(
        "若已连接 cognitive-enzyme MCP：验证后请调 report(enzyme_ids=[上列 id 按序], success) "
        "反馈以强化/纠正；任务中途需要更多相关经验可调 search(query) 主动检索。"
    )
    lines.append("⟦CE:END⟧")
    return "\n".join(lines)


def build_prompt(task: dict, memory_ids: list[str], corpus: dict[str, dict]) -> str:
    mems = [corpus[i] for i in memory_ids if i in corpus]
    prompt = f"{render_block(mems)}\n\n{task['prompt']}" if mems else task["prompt"]
    return (" " + prompt) if prompt.startswith("@") else prompt    # pi 把 @ 开头的参数当文件


# ── 判分 ───────────────────────────────────────────────────────

_ANSWER_LINE = re.compile(r"(?im)^[\s>*_#`-]*(?:final\s+)?answer[\s*_`]*[:：]\s*(.+?)\s*$")


def final_answer(text: str) -> str | None:
    hits = _ANSWER_LINE.findall(text or "")
    return hits[-1].strip() if hits else None


def _strip(s: str) -> str:
    s = re.sub(r"\\boxed\{(.*)\}", r"\1", s)
    s = s.replace("$", "").replace("**", "").replace("`", "").strip()
    if "≈" in s:
        s = s.split("≈")[-1]
    if "=" in s and not s.strip().startswith("["):
        s = s.split("=")[-1]
    return s.strip().rstrip(".").strip()


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_NAMES = {"pi": math.pi, "e": math.e}
_FUNCS = {"sqrt": math.sqrt, "ln": math.log, "log": math.log, "exp": math.exp}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _NAMES:
        return _NAMES[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and len(node.args) == 1:
        return _FUNCS[node.func.id](_eval(node.args[0]))
    raise ValueError("unsupported")


def parse_number(s: str) -> float | None:
    s = _strip(s)
    s = re.sub(r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"(\1)/(\2)", s)
    s = s.replace("\\pi", "pi").replace("π", "pi").replace("\\cdot", "*").replace("×", "*").replace("\\times", "*")
    s = s.replace("\\sqrt", "sqrt").replace("√", "sqrt").replace("{", "(").replace("}", ")").replace("^", "**")
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)
    s = s.rstrip("%").strip()           # 百分数:判分时 45% 按 45 和 0.45 都算(题目口径不统一)
    m = re.match(r"^[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
    candidates = [s]
    if m:
        candidates.append(m.group(0))       # "12.5 m/s" → 12.5
    for cand in candidates:
        cand = re.sub(r"(\d)\s*(pi|sqrt|\()", r"\1*\2", cand)
        try:
            return _eval(ast.parse(cand, mode="eval"))
        except Exception:
            continue
    return None


def _close(pred: float, gt: float, pct: bool = False) -> bool:
    ok = abs(pred - gt) <= (abs(gt) * 0.04 if gt else 1e-6)
    return ok or (pct and _close(pred / 100, gt))


def score_theorem(task: dict, text: str) -> dict:
    raw = final_answer(text)
    if raw is None:
        return {"pred": None, "correct": False, "parsed": False}
    gt, kind = task["answer"], task["answer_type"]
    if kind == "bool":
        w = _strip(raw).lower().split()[0].strip(".,;:()") if _strip(raw) else ""
        pred = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(w)
        return {"pred": pred, "correct": pred is not None and pred == bool(gt), "parsed": pred is not None}
    if kind == "option":
        m = re.search(r"\(?([a-eA-E])\)?", _strip(raw))
        pred = f"({m.group(1).lower()})" if m else None
        return {"pred": pred, "correct": pred == str(gt).strip().lower(), "parsed": pred is not None}
    if kind.startswith("list"):
        m = re.search(r"\[(.*?)\]", raw) or re.search(r"\((.*?)\)", raw)
        parts = [parse_number(p) for p in (m.group(1) if m else _strip(raw)).split(",") if p.strip()]
        gt_list = gt if isinstance(gt, list) else json.loads(str(gt))
        if not parts or any(p is None for p in parts):
            return {"pred": raw[:80], "correct": False, "parsed": False}
        ok = len(parts) == len(gt_list) and all(_close(p, float(g)) for p, g in zip(parts, gt_list))
        return {"pred": parts, "correct": ok, "parsed": True}
    pred = parse_number(raw)
    if pred is None:
        return {"pred": raw[:80], "correct": False, "parsed": False}
    return {"pred": pred, "correct": _close(pred, float(gt), pct="%" in raw), "parsed": True}


def score_security(task: dict, text: str) -> dict:
    raw = final_answer(text) or ""
    ids = re.findall(r"CWE[-\s_]?(\d+)", raw, re.I) or re.findall(r"CWE[-\s_]?(\d+)", text or "", re.I)[-1:]
    if not ids:
        return {"pred": None, "correct": False, "lenient": False, "parsed": False}
    pred = f"cwe-{ids[0]}"
    return {"pred": pred, "correct": pred in task["answer"],
            "lenient": pred in task["answer"] or pred in task["acceptable"], "parsed": True}


def score_biomed(task: dict, text: str) -> dict:
    raw = (final_answer(text) or "").upper()
    pred = None
    if re.search(r"CONTRADICT|REFUT", raw):
        pred = "CONTRADICT"
    elif re.search(r"SUPPORT", raw):
        pred = "SUPPORT"
    return {"pred": pred, "correct": pred == task["answer"], "parsed": pred is not None}


def score_firefox(task: dict, text: str) -> dict:
    raw = final_answer(text)
    if raw is None:
        return {"pred": None, "correct": False, "abstain": False, "parsed": False}
    m = re.search(r"\b(\d{5,8})\b", raw)
    if not m:
        abstain = bool(re.search(r"\bNEW\b|UNKNOWN|NONE|NOT A DUPLICATE", raw.upper()))
        return {"pred": "NEW" if abstain else raw[:80], "correct": False, "abstain": abstain, "parsed": abstain}
    return {"pred": m.group(1), "correct": m.group(1) in task["answer"], "abstain": False, "parsed": True}


_CASE_STOP = {
    "the", "of", "and", "in", "re", "ex", "rel", "et", "al", "united", "states", "state", "people", "america",
    "inc", "co", "corp", "corporation", "company", "companies", "llc", "ltd", "lp", "na", "city", "county",
    "town", "commonwealth", "commissioner", "comm'r", "department", "dept", "board", "bank", "insurance",
    "national", "american", "association", "assn", "matter", "estate", "government",
}


def _parties(name: str) -> list[set[str]]:
    """案名 → 每一方当事人的特征词(去掉 United States、Inc.、Co. 这类通用词)。"""
    name = re.sub(r",\s*\d.*$", "", name or "").lower()          # 去掉卷页引注
    sides = re.split(r"\s+(?:v|vs)\.?\s+", name)
    return [{w.strip("'’-") for w in re.findall(r"[a-z][a-z'’-]+", s)} - _CASE_STOP for s in sides]


def same_case(pred: str, gold: str) -> bool:
    """双方当事人都至少对上一个特征词;某一方全是通用词(如 United States)时只比另一方。"""
    p, g = _parties(pred), _parties(gold)
    pairs = [(ps, gs) for ps, gs in zip(p, g) if gs]
    if len(p) != len(g) or not pairs:
        flat_p, flat_g = set().union(*p), set().union(*g)
        return bool(flat_g) and len(flat_p & flat_g) >= min(2, len(flat_g))
    return all(ps & gs for ps, gs in pairs)


def score_legal(task: dict, text: str) -> dict:
    raw = final_answer(text)
    if raw is None:
        return {"pred": None, "correct": False, "abstain": False, "parsed": False}
    pred = _strip(raw)
    if re.fullmatch(r"(?i)unknown|none|n/?a|cannot (?:be )?(?:identified|determine)d?.*", pred) or not pred:
        return {"pred": "UNKNOWN", "correct": False, "abstain": True, "parsed": True}
    ok = any(same_case(pred, name) for name in [task["answer"], *task.get("acceptable", [])])
    return {"pred": pred[:120], "correct": ok, "abstain": False, "parsed": True}


SCORERS = {"theorem": score_theorem, "security": score_security, "biomed": score_biomed,
           "firefox": score_firefox, "legal": score_legal}


def adopted_wrong(task: dict, row: dict, injected: list[str], corpus: dict[str, dict]) -> bool:
    """答错了,而且答案正是某条注入的错记忆——被带偏。"""
    if row.get("correct") or row.get("abstain") or not row.get("pred"):
        return False
    wrong = [i for i in injected if i not in task["gold_ids"]]
    d, pred = task["domain"], str(row["pred"])
    if d == "firefox":
        return pred in {i.removeprefix("bz-") for i in wrong}
    if d == "security":
        return pred in wrong
    if d == "legal":
        return any(same_case(pred, corpus[i].get("case", "")) for i in wrong if i in corpus)
    return False


# ── 调 pi ─────────────────────────────────────────────────────

def api_key(provider: str) -> str:
    if not provider:
        return ""
    r = subprocess.run(["pi", "auth", "print-api-key", "--provider", provider],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    key = r.stdout.strip()
    if r.returncode != 0 or not key:
        raise SystemExit(f"pi 里没有服务商 {provider} 的密钥(pi auth check --provider {provider})")
    return key


def parse_events(stdout: str) -> dict:
    last = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "message_end" and (e.get("message") or {}).get("role") == "assistant":
            last = e["message"]
    if last is None:
        return {"ok": False, "error": "no assistant message"}
    content = last.get("content") or []
    text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
    thinking = sum(len(c.get("thinking", "")) for c in content if c.get("type") == "thinking")
    stop = last.get("stopReason")
    usage = last.get("usage") or {}
    out = {"ok": stop in ("stop", "length"), "text": text, "thinking_chars": thinking, "stop": stop,
           "usage": {k: usage.get(k) for k in ("input", "output", "reasoning", "cacheRead")}}
    if not out["ok"]:
        out["error"] = (last.get("errorMessage") or f"stop={stop}")[:500]
    return out


class GlobalSlots:
    """跨进程的并发上限:同一个智谱 Coding Plan 账号同时在飞的请求超过 5 个左右会被 1302 限流,
    几个评测进程共用这些槽位(文件锁;进程退出时操作系统自动释放)。槽位数取环境变量 CE_GLM_SLOTS,默认 5。"""

    def __init__(self, n: int, directory: Path):
        self.n, self.dir = n, directory

    @contextlib.asynccontextmanager
    async def hold(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        while True:
            for k in random.sample(range(self.n), self.n):
                fd = os.open(self.dir / f"slot{k}", os.O_CREAT | os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                return
            await asyncio.sleep(1 + random.random())


SLOTS = GlobalSlots(int(os.environ.get("CE_GLM_SLOTS", "5")), CACHE / "glm_slots")
_OPENCODE_VERSIONS: dict[str, str] = {}


async def ask_pi(prompt: str, args, env: dict, cwd: Path) -> dict:
    cmd = ["pi", "-p", "--mode", "json", "--no-session", "-nt", "-ne", "-ns", "-np", "-nc", "--offline",
           "--thinking", args.thinking, "--model", args.model, "--", prompt]
    async with SLOTS.hold():
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    cwd=cwd, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), args.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"timeout {args.timeout:.0f}s", "secs": round(time.perf_counter() - t0, 1)}
        secs = round(time.perf_counter() - t0, 1)
    res = parse_events(out.decode("utf-8", "replace"))
    res["secs"] = secs                                # 只算作答本身,不含排队等槽位的时间
    if not res["ok"] and proc.returncode:
        res["error"] = f"{res.get('error', '')} | exit {proc.returncode}: {err.decode('utf-8', 'replace')[-300:]}"
    return res


def parse_opencode(stdout: str) -> dict:
    """Read OpenCode's documented --format json events, never run tool calls."""
    parts, reasons, finishes, errors = [], [], [], []
    usage = {"input": 0, "output": 0, "reasoning": 0, "cacheRead": 0}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind, part = event.get("type"), event.get("part") or {}
        if kind == "text":
            parts.append(part.get("text", ""))
        elif kind == "reasoning":
            reasons.append(part.get("text", ""))
        elif kind == "tool_use":
            errors.append("tool use invalidates closed-book evaluation")
        elif kind == "error":
            error = event.get("error") or {}
            errors.append(str((error.get("data") or {}).get("message") or error.get("message") or error)[:500])
        elif kind == "step_finish":
            finishes.append(part.get("reason"))
            tokens = part.get("tokens") or {}
            for key in ("input", "output", "reasoning"):
                usage[key] += tokens.get(key) or 0
            usage["cacheRead"] += (tokens.get("cache") or {}).get("read") or 0
    text = "".join(parts)
    ok = not errors and bool(finishes) and (finishes[-1] == "length" or (finishes[-1] == "stop" and bool(text.strip())))
    return {"ok": ok, "text": text, "thinking_chars": sum(map(len, reasons)),
            "stop": finishes[-1] if finishes else None, "usage": usage,
            **({"error": "; ".join(errors) or "no completed assistant answer"} if not ok else {})}


async def ask_opencode(prompt: str, args, env: dict, cwd: Path) -> dict:
    executable = getattr(args, "opencode_bin", "") or shutil.which("opencode")
    bundled = Path("/Applications/OpenCode.app/Contents/Resources/opencode-cli")
    if not executable and bundled.is_file():
        executable = str(bundled)
    if not executable:
        return {"ok": False, "error": "opencode executable missing; pass --opencode-bin", "secs": 0}
    if executable not in _OPENCODE_VERSIONS:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=20)
        _OPENCODE_VERSIONS[executable] = version.stdout.strip()
    v2 = bool(re.search(r"\bv2\.", _OPENCODE_VERSIONS[executable]))
    if v2:
        from tests.eval.opencode_runner import ask
        return await ask(prompt, args, env, executable)
    child_env = dict(env)
    for name in ("AUTOUPDATE", "DEFAULT_PLUGINS", "LSP_DOWNLOAD", "CLAUDE_CODE", "AUTOCOMPACT"):
        child_env[f"OPENCODE_DISABLE_{name}"] = "true"
    child_env["OPENCODE_AUTO_SHARE"] = "false"
    child_env["OPENCODE_PERMISSION"] = json.dumps({"*": "deny"})
    for kind in ("CONFIG", "DATA", "CACHE"):
        child_env[f"XDG_{kind}_HOME"] = str(cwd / f"opencode-{kind.lower()}")
    config = {
        "permission": {"*": "deny"}, "plugin": [],
        "agent": {"ce-eval": {"mode": "primary", "prompt":
            "You answer the user's question using only the provided text. Do not use tools, files, external sources, or delegate. Follow the requested final-answer format.",
            "permission": {"*": "deny"}, "tools": {"*": False},
            "options": {"maxOutputTokens": getattr(args, "max_output_tokens", 16384)}}},
    }
    if args.model == "opencode/deepseek-v4.1-flash-free":
        # The provider's live catalogue can be ahead of the CLI's bundled models.
        config["provider"] = {"opencode": {"models": {"deepseek-v4.1-flash-free": {
            "name": "DeepSeek V4.1 Flash Free", "reasoning": True,
            "limit": {"context": 1048576, "output": 65536},
            "cost": {"input": 0, "output": 0},
        }}}}
    child_env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    t0 = time.perf_counter()
    # Keep the working directory outside the repository so its context files
    # cannot be loaded into the closed-book solver. No credentials are copied.
    with tempfile.TemporaryDirectory(prefix="ce-opencode-eval-") as clean_cwd:
        child_env["PWD"] = clean_cwd
        cmd = [executable, "run", "--dir", clean_cwd, "--format", "json", "--agent", "ce-eval", "--model", args.model, "--", prompt]
        proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=clean_cwd, env=child_env,
            start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), args.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"ok": False, "error": f"timeout {args.timeout:.0f}s", "secs": round(time.perf_counter() - t0, 1)}
    result = parse_opencode(out.decode("utf-8", "replace"))
    result["secs"] = round(time.perf_counter() - t0, 1)
    if proc.returncode:
        detail = err.decode('utf-8', 'replace').strip() or out.decode('utf-8', 'replace').strip()
        result.update(ok=False, error=f"opencode exit {proc.returncode}: {detail[-1000:]}")
    return result


async def ask_openai(prompt: str, args, env: dict, cwd: Path) -> dict:
    """Use an existing local provider; keep credentials out of rows and logs."""
    import httpx
    config = Path(getattr(args, "provider_config", "~/.pi/agent/models.json")).expanduser()
    provider_name = getattr(args, "provider_name", "magpie")
    try:
        provider = json.loads(config.read_text())["providers"][provider_name]
        key = provider.get("apiKey", "")
        if not isinstance(key, str):
            raise ValueError("provider apiKey must be a string")
        url = provider["baseUrl"].rstrip("/") + "/chat/completions"
    except (OSError, KeyError, ValueError) as exc:
        return {"ok": False, "error": f"provider configuration unavailable: {type(exc).__name__}", "secs": 0}
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=args.timeout) as client:
            payload = {
                "model": args.model, "messages": [
                    {"role": "system", "content": "Answer using only the supplied text. Do not use tools or external sources. Follow the requested final-answer format."},
                    {"role": "user", "content": prompt}],
                "max_tokens": getattr(args, "max_output_tokens", 16384),
            }
            if "step-" in args.model:
                payload["reasoning_effort"] = "low" if getattr(args, "thinking", "low") == "off" else args.thinking
            response = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        if not response.is_success:
            return {"ok": False, "error": f"HTTP {response.status_code}: {response.text[:500]}",
                    "secs": round(time.perf_counter() - t0, 1)}
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = data.get("usage") or {}
        tokens = usage.get("completion_tokens_details") or {}
        ok = (isinstance(text, str) and not message.get("tool_calls")
              and (choice.get("finish_reason") == "length"
                   or (choice.get("finish_reason") == "stop" and bool(text.strip()))))
        return {"ok": ok, "text": text, "stop": choice.get("finish_reason"),
                "served_model": data.get("model"), "thinking_chars": len(message.get("reasoning_content") or ""),
                "secs": round(time.perf_counter() - t0, 1),
                "usage": {"input": usage.get("prompt_tokens"), "output": usage.get("completion_tokens"),
                          "reasoning": tokens.get("reasoning_tokens"),
                          "cacheRead": (usage.get("prompt_tokens_details") or {}).get("cached_tokens")},
                **({"error": "no final answer or unexpected tool call"} if not ok else {})}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__, "secs": round(time.perf_counter() - t0, 1)}


async def ask_with_retry(prompt: str, args, env: dict, cwd: Path, log) -> dict:
    """限流 / 额度用尽:退避等待后重试(最多等 args.max_wait 秒);其他错误重试 3 次。"""
    tries, waited, delay = 0, 0.0, 60.0
    while True:
        runner = {"opencode": ask_opencode, "openai": ask_openai}.get(getattr(args, "runner", "pi"), ask_pi)
        res = await runner(prompt, args, env, cwd)
        if res["ok"]:
            res["retries"] = tries
            return res
        if res.get("timed_out") and getattr(args, "purpose", "") == "solve":
            res.update(ok=True, text="", stop="time_budget", budget_exhausted=True,
                       budget_error=res.pop("error", "time budget exceeded"), retries=tries)
            return res
        if res.get("protocol_error"):
            # A model's attempt to use forbidden tools is an unsuccessful answer,
            # not a service outage that earns additional sampled attempts.
            res.update(ok=True, text="", stop="protocol_violation", retries=tries)
            return res
        if re.search(r"free tier|FreeTierError|HTTP (?:401|403)", res.get("error", ""), re.I):
            res["retries"] = tries
            return res  # changing credentials/client setup is required; retrying will not help
        tries += 1
        if RATE_LIMIT.search(res.get("error", "")) and waited < args.max_wait:
            pause = min(delay, args.max_wait - waited)
            log(f"    限流/额度:{res['error'][:160]} → 等 {pause:.0f}s")
            await asyncio.sleep(pause)
            waited += pause
            delay = min(delay * 2, 900.0)
            continue
        log(f"    失败(第 {tries} 次,{res.get('secs')}s):{res.get('error', '')[:160]}")
        if tries > getattr(args, "service_retries", 3):
            res["retries"] = tries - 1
            return res
        await asyncio.sleep(5 * tries)


# ── 主流程 ─────────────────────────────────────────────────────

def jobs_for(task: dict, injections: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    jobs = [("none", [])]
    injected = injections.get(task["id"]) or []
    if injected:
        jobs.append(("system", injected))
    jobs.append(("gold", task["gold_ids"]))
    return jobs


def result_path(tag: str) -> Path:
    return OUT / f"uplift_{tag}.jsonl"


def load_rows(path: Path) -> dict[tuple[str, str], dict]:
    rows = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[(r["qid"], r["arm"])] = r       # 同一对后写的覆盖先写的(重跑出错的行)
    return rows


async def run(args) -> None:
    args.purpose = "solve"
    domains = tuple(d for d in args.domains.split(",") if d)
    tasks = load_tasks(domains)
    if args.limit:
        per: dict[str, int] = defaultdict(int)
        kept = []
        for t in tasks:
            if per[t["domain"]] < args.limit:
                kept.append(t)
                per[t["domain"]] += 1
        tasks = kept
    corpus = load_corpus(domains)
    injections = load_system_injections()
    OUT.mkdir(parents=True, exist_ok=True)
    path = result_path(args.tag)
    log_path = OUT / f"uplift_{args.tag}.log"
    done = {k for k, r in load_rows(path).items() if not r.get("error")} if args.resume else set()
    if not args.resume and path.exists():
        raise SystemExit(f"{path} 已存在:加 --resume 续跑,或换 --tag")
    identity = {"protocol": "uplift-v2-closed-book", "runner": args.runner, "model": args.model,
                "thinking": args.thinking, "max_output_tokens": args.max_output_tokens, "timeout": args.timeout,
                "tasks_sha256": hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest(),
                "task_ids": [t["id"] for t in tasks],
                "injections_sha256": hashlib.sha256(json.dumps(injections, sort_keys=True).encode()).hexdigest(),
                "corpus_sha256": hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest()}
    if args.runner == "opencode":
        from tests.eval.opencode_runner import CLOSED_BOOK
        identity["closed_book_sha256"] = hashlib.sha256(CLOSED_BOOK.encode()).hexdigest()
    manifest = OUT / f"uplift_{args.tag}.manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text()) != identity:
            raise SystemExit("模型、预算或冻结输入不同;请换 --tag,禁止混合续跑")
    elif path.exists():
        raise SystemExit("历史结果没有 manifest,只能 --report;新评测请换 --tag")
    else:
        manifest.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n")

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    env = dict(os.environ, PI_OFFLINE="1", PI_TELEMETRY="0")
    if args.runner == "pi" and args.key_from:
        env[args.key_env] = api_key(args.key_from)
    cwd = OUT / "pi_cwd"
    cwd.mkdir(exist_ok=True)

    todo = [(t, arm, ids) for t in tasks for arm, ids in jobs_for(t, injections) if (t["id"], arm) not in done]
    log(f"tag={args.tag} model={args.model} thinking={args.thinking} 题目 {len(tasks)} "
        f"({', '.join(f'{d} {sum(t['domain'] == d for t in tasks)}' for d in domains)}) · 待跑 {len(todo)} 次")
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    counter = {"n": 0, "err": 0}

    async def one(task: dict, arm: str, ids: list[str]) -> None:
        async with sem:
            res = await ask_with_retry(build_prompt(task, ids, corpus), args, env, cwd, log)
        row = {"qid": task["id"], "domain": task["domain"], "arm": arm, "injected": ids,
               "hit": bool(set(ids) & (set(task["gold_ids"]) | set(task.get("acceptable") or []))),
               "secs": res.get("secs"), "retries": res.get("retries", 0), "usage": res.get("usage"),
               "thinking_chars": res.get("thinking_chars"), "stop": res.get("stop"),
               "model": args.model, "runner": args.runner, "served_model": res.get("served_model"),
               "session_id": res.get("session_id"), "cost": res.get("cost"),
               "protocol_error": res.get("protocol_error"), "text": res.get("text") or ""}
        if res["ok"]:
            row.update(SCORERS[task["domain"]](task, res.get("text") or ""))
            row["misled"] = adopted_wrong(task, row, ids, corpus)
        else:
            row["error"] = res.get("error", "unknown")
        async with lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            counter["n"] += 1
            counter["err"] += bool(row.get("error"))
            mark = "错误" if row.get("error") else ("对" if row.get("correct") else "错")
            log(f"  [{counter['n']}/{len(todo)}] {task['domain']:<8} {task['id']:<24} {arm:<6} {mark} "
                f"{row.get('secs')}s pred={str(row.get('pred'))[:40]}"
                + (f" err={row['error'][:120]}" if row.get("error") else ""))

    await asyncio.gather(*(one(t, arm, ids) for t, arm, ids in todo))
    log(f"完成:{counter['n']} 次,出错 {counter['err']} 次")
    report(args.tag, tasks, injections)


# ── 报表 ───────────────────────────────────────────────────────

def mcnemar_p(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def report(tag: str, tasks: list[dict] | None = None, injections: dict | None = None) -> str:
    rows = load_rows(result_path(tag))
    domains = tuple(d for d in DOMAINS if any(r["domain"] == d for r in rows.values()))
    tasks = tasks or load_tasks(domains)
    injections = injections if injections is not None else load_system_injections()
    lines = [f"# 注入的真实效果:{tag}", ""]
    manifest = OUT / f"uplift_{tag}.manifest.json"
    if manifest.exists():
        meta = json.loads(manifest.read_text())
        lines += [f"模型：{meta['runner']} / {meta['model']}；冻结题目 {len(meta['task_ids'])}。", ""]
    errors = [r for r in rows.values() if r.get("error")]
    lines.append(f"作答 {len(rows)} 次,出错 {len(errors)} 次,限流重试后仍失败的不计分。")
    lines.append("")
    lines.append("| 领域 | 题数 | 不注入 | 系统注入 | 只注入正确记忆 | 系统拿到的比例 | 解析失败 |")
    lines.append("|---|---|---|---|---|---|---|")
    detail, makeup = [], []
    for d in domains:
        ts = [t for t in tasks if t["domain"] == d]
        paired = []
        for t in ts:
            n, g = rows.get((t["id"], "none")), rows.get((t["id"], "gold"))
            injected = bool(injections.get(t["id"]))
            s = rows.get((t["id"], "system")) if injected else n
            if not n or not g or not s or any(r.get("error") for r in (n, g, s)):
                continue
            paired.append((t, n, s, g, injected))
        if not paired:
            continue
        keys = ["correct"] + (["lenient"] if d == "security" else [])
        for key in keys:
            acc = {arm: sum(bool(x[i].get(key)) for x in paired) / len(paired)
                   for arm, i in (("none", 1), ("system", 2), ("gold", 3))}
            gain = acc["gold"] - acc["none"]
            share = f"{(acc['system'] - acc['none']) / gain:.0%}" if gain > 0.005 else "—"
            fails = sum(not x[i].get("parsed") for x in paired for i in (1, 2, 3))
            name = d if key == "correct" else f"{d}(宽松)"
            lines.append(f"| {name} | {len(paired)} | {acc['none']:.1%} | {acc['system']:.1%} | {acc['gold']:.1%} "
                         f"| {share} | {fails} |")
        # 配对翻转:b = 不注入错 → 该条件对;c = 不注入对 → 该条件错
        for label, subset, idx in (
            ("只注入正确记忆 vs 不注入", paired, 3),
            ("系统注入 vs 不注入(有注入的题)", [x for x in paired if x[4]], 2),
            ("  其中注入里有正确记忆", [x for x in paired if x[4] and x[2].get("hit")], 2),
            ("  其中注入的全是错记忆", [x for x in paired if x[4] and not x[2].get("hit")], 2),
        ):
            b = sum((not x[1].get("correct")) and bool(x[idx].get("correct")) for x in subset)
            c = sum(bool(x[1].get("correct")) and not x[idx].get("correct") for x in subset)
            detail.append(f"| {d} | {label} | {len(subset)} | {b} | {c} | {mcnemar_p(b, c):.3f} |")
        # 答案构成:答对 / 给了具体但错的答案 / 答不知道;系统注入组里错答案正好取自某条错记忆 = 被带偏
        for arm, i in (("none", 1), ("system", 2), ("gold", 3)):
            xs = [x for x in paired if x[4]] if arm == "system" else paired
            if not xs:
                continue
            ok = sum(bool(x[i].get("correct")) for x in xs)
            abstain = sum(bool(x[i].get("abstain")) for x in xs)
            misled = sum(bool(x[i].get("misled")) for x in xs)
            wrong = len(xs) - ok - abstain
            makeup.append(f"| {d} | {arm}{'(有注入的题)' if arm == 'system' else ''} | {len(xs)} | {ok} | {wrong} "
                          f"| {abstain} | {misled if arm == 'system' else '—'} |")
    lines += ["", "配对翻转(同一道题两种条件的对错变化;p 为 McNemar 精确检验):", "",
              "| 领域 | 比较 | 题数 | 变对 | 变错 | p |", "|---|---|---|---|---|---|", *detail]
    lines += ["", "答案构成(被带偏 = 错答案正好取自注入的某条错记忆):", "",
              "| 领域 | 条件 | 题数 | 答对 | 答错 | 答不知道 | 被带偏 |", "|---|---|---|---|---|---|---|", *makeup]
    secs = [r["secs"] for r in rows.values() if r.get("secs") and not r.get("error")]
    if secs:
        secs.sort()
        lines += ["", f"每次作答耗时中位数 {secs[len(secs) // 2]}s,最长 {secs[-1]}s;"
                      f"重试 {sum(r.get('retries', 0) for r in rows.values())} 次。"]
    text = "\n".join(lines)
    (OUT / f"uplift_{tag}.md").write_text(text + "\n", encoding="utf-8")
    print(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--domains", default=",".join(DEFAULT_DOMAINS), help=f"可选:{','.join(DOMAINS)}")
    ap.add_argument("--limit", type=int, default=0, help="每个领域最多几道题(试跑用)")
    ap.add_argument("--model", default="zai-coding-cn/glm-5.3")
    ap.add_argument("--runner", choices=("pi", "opencode", "openai"), default="pi")
    ap.add_argument("--opencode-bin", default=os.environ.get("CE_EVAL_OPENCODE_BIN", ""))
    ap.add_argument("--max-output-tokens", type=int, default=32000)
    ap.add_argument("--provider-config", default="~/.pi/agent/models.json")
    ap.add_argument("--provider-name", default="magpie")
    ap.add_argument("--thinking", default="high")
    ap.add_argument("--key-from", default="cc-switch-zhipu-glm", help="从 pi 里哪个服务商取密钥;留空则用现有环境变量")
    ap.add_argument("--key-env", default="ZAI_CODING_CN_API_KEY")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=900.0, help="单次作答超时(秒)")
    ap.add_argument("--max-wait", type=float, default=6 * 3600.0, help="单题因限流累计最多等多久(秒)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--report", action="store_true", help="只根据已有结果出报表")
    args = ap.parse_args()
    if args.report:
        report(args.tag)
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
