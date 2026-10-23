"""老师擂台核心:同一批判定交给不同判官作答(带缓存),按真实答案算指标、出报表。

判定(decision)格式:{"id", "domain", "kind": positive/silent/unlabeled, "state", "questions",
"candidates": [记忆 id], "positive": [题号], "expected": [记忆 id], "logged": {判官: 作答},
"floor_ok": [没被精排淘汰的记忆 id]} —— app.distill.export --format arena 的输出,或评测落盘还原。

指标(整体 + 分领域):
  noul AUC / Brier / ECE —— 逐条"是否有用"对真实答案的区分度与校准
  choice top1           —— 单选最高项(不含 none)是正确记忆的比例(正例)
  silence               —— 静默查询里单选判 none 为最高项的比例
  selection R/P/S       —— 套用线上默认选择规则后的召回 / 精度 / 静默(阈值按 Jev 标定,只作参考)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections import defaultdict
from pathlib import Path

from app.matching.selection import select

log = logging.getLogger("arena")


def decisions_from_jsonl(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def sample(decisions: list[dict], per_domain: int, seed: int) -> list[dict]:
    if per_domain <= 0:
        return decisions
    rng = random.Random(seed)
    by = defaultdict(list)
    for d in decisions:
        by[d["domain"]].append(d)
    out = []
    for dom in sorted(by):
        items = by[dom]
        out += rng.sample(items, min(per_domain, len(items)))
    return out


# ── 作答(带缓存)──────────────────────────────────────────────

def _hash(d: dict) -> str:
    return hashlib.sha1(json.dumps([d["state"], d["questions"]], ensure_ascii=False, sort_keys=True)
                        .encode()).hexdigest()


async def run_teacher(name: str, teacher, decisions: list[dict], cache_root: Path) -> dict[str, dict]:
    cache_dir = cache_root / name
    cache_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(teacher.spec.concurrency)
    results: dict[str, dict] = {}
    stats = {"cached": 0, "ok": 0, "failed": 0, "tokens": 0, "latency": []}

    async def one(d):
        f = cache_dir / f"{_hash(d)}.json"
        if f.exists():
            results[d["id"]] = json.loads(f.read_text(encoding="utf-8"))["answers"]
            stats["cached"] += 1
            return
        async with sem:
            ans = await teacher.answer(d["state"], d["questions"])
        if ans.answers is None:
            stats["failed"] += 1
            log.warning("[%s] %s failed: %s", name, d["id"], ans.error[:200])
            return
        f.write_text(json.dumps({"answers": ans.answers, "usage": ans.usage, "latency_s": ans.latency_s},
                                ensure_ascii=False), encoding="utf-8")
        results[d["id"]] = ans.answers
        stats["ok"] += 1
        stats["tokens"] += int(ans.usage.get("total_tokens") or 0)
        stats["latency"].append(ans.latency_s)
        done = stats["ok"] + stats["failed"]
        if done % 10 == 0:
            log.info("[%s] %d new answers (%d failed), %d cached", name, stats["ok"], stats["failed"], stats["cached"])

    await asyncio.gather(*(one(d) for d in decisions))
    lat = sorted(stats["latency"])
    log.info("[%s] done: ok=%d cached=%d failed=%d tokens=%d p50=%.1fs", name, stats["ok"], stats["cached"],
             stats["failed"], stats["tokens"], lat[len(lat) // 2] if lat else 0.0)
    return results


# ── 指标 ────────────────────────────────────────────────────────

def _auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    pairs = [(s, 1) for s in pos] + [(s, 0) for s in neg]
    pairs.sort()
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        rank_sum += avg * sum(1 for k in range(i, j) if pairs[k][1] == 1)
        i = j
    return (rank_sum - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _ece(pairs: list[tuple[float, int]], bins: int = 10) -> float | None:
    if not pairs:
        return None
    total, err = len(pairs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        cell = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if cell:
            err += len(cell) / total * abs(sum(p for p, _ in cell) / len(cell) - sum(y for _, y in cell) / len(cell))
    return err


def _select(d: dict, ans: dict) -> list[int]:
    """线上核心选择规则(app/matching/selection.py,与 pipeline 同源;不含危急级/类比/晋升/探索)。"""
    n = len(d["candidates"])
    # 记录里可能有显式 null 的作答("c3": null):按空作答处理,别把整轮 metrics 炸掉
    p = {f"c{i}": (ans.get(f"c{i}") or {}).get("noul", 0.0) for i in range(n)}
    probs = (ans.get("best") or {}).get("probabilities", {}) or {}
    q = {k: probs.get(k, 0.0) for k in p}
    floor_ok = set(d.get("floor_ok", d["candidates"]))
    rescued = {f"c{i}" for i, e in enumerate(d["candidates"]) if e not in floor_ok}
    return [int(k[1:]) for k in select(p, q, probs.get("none", 0.0), rescued)]


def metrics(decisions: list[dict], answers: dict[str, dict]) -> dict:
    pos, neg, cal = [], [], []
    top1_n = top1_ok = sil_n = sil_ok = 0
    et = eh = it = ir = sel_sil_n = sel_sil_ok = 0
    answered = 0
    for d in decisions:
        ans = answers.get(d["id"])
        if not ans:
            continue
        answered += 1
        gold = set(d["positive"])
        for i in range(len(d["candidates"])):
            a = ans.get(f"c{i}")
            if a is None or a.get("noul") is None:
                continue
            y = 1 if f"c{i}" in gold else 0
            (pos if y else neg).append(a["noul"])
            cal.append((a["noul"], y))
        probs = ans.get("best", {}).get("probabilities", {})
        if probs:
            opts = {k: v for k, v in probs.items() if k != "none"}
            if d["kind"] == "positive" and gold and opts:
                top1_n += 1
                top1_ok += max(opts, key=opts.get) in gold
            if d["kind"] == "silent":
                sil_n += 1
                sil_ok += max(probs, key=probs.get) == "none"
        sel = {f"c{i}" for i in _select(d, ans)}
        if d["kind"] == "silent":
            sel_sil_n += 1
            sel_sil_ok += not sel
        else:
            exp = {f"c{i}" for i, e in enumerate(d["candidates"]) if e in set(d["expected"])}
            et += len(d["expected"])
            eh += len(exp & sel)
            it += len(sel)
            ir += len(gold & sel)
    r = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    auc = _auc(pos, neg)
    return {"answered": answered, "noul_auc": round(auc, 4) if auc is not None else None,
            "brier": r(sum((p - y) ** 2 for p, y in cal), len(cal)),
            "ece": round(_ece(cal), 4) if cal else None,
            "choice_top1": r(top1_ok, top1_n), "choice_silence": r(sil_ok, sil_n),
            "sel_recall": r(eh, et), "sel_precision": r(ir, it) if it else (1.0 if et else None),
            "sel_silence": r(sel_sil_ok, sel_sil_n)}


def report(name: str, decisions: list[dict], per_teacher: dict[str, dict[str, dict]], out_dir: Path) -> str:
    doms = sorted({d["domain"] for d in decisions})
    rows = {t: {"all": metrics(decisions, a), **{dom: metrics([d for d in decisions if d["domain"] == dom], a)
                                                  for dom in doms}} for t, a in per_teacher.items()}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"arena_{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    fmt = lambda v: "-" if v is None else (f"{100 * v:.1f}%" if isinstance(v, float) and v <= 1 else str(v))  # noqa: E731
    lines = [f"# 老师擂台: {name}", "", f"判定 {len(decisions)} 次(按领域:" +
             ", ".join(f"{dom} {sum(d['domain'] == dom for d in decisions)}" for dom in doms) + ")", "",
             "| 老师 | 作答 | noul AUC | Brier | ECE | 单选 top1 | 单选静默 | 选择 R / P / 静默 |",
             "|---|---|---|---|---|---|---|---|"]
    for t, m in rows.items():
        a = m["all"]
        lines.append(f"| {t} | {a['answered']} | {a['noul_auc']} | {a['brier']} | {a['ece']} | {fmt(a['choice_top1'])} | "
                     f"{fmt(a['choice_silence'])} | {fmt(a['sel_recall'])} / {fmt(a['sel_precision'])} / "
                     f"{fmt(a['sel_silence'])} |")
    lines += ["", "## 分领域(noul AUC / 单选 top1 / 选择 R-P)", "", "| 老师 | " + " | ".join(doms) + " |",
              "|---|" + "---|" * len(doms)]
    for t, m in rows.items():
        cells = [f"{m[dom]['noul_auc']} / {fmt(m[dom]['choice_top1'])} / {fmt(m[dom]['sel_recall'])}-"
                 f"{fmt(m[dom]['sel_precision'])}" for dom in doms]
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    (out_dir / f"arena_{name}.md").write_text(text, encoding="utf-8")
    return text
