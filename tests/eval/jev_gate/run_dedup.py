"""记忆去重判定:Jev noul("两条是否给出实质相同的建议")vs 现行 merge_similar 规则。

合并是破坏性的(被吸收的酶会被删除),误合并 = 丢记忆,所以主指标是**精度**。
候选对 = 语料里向量最近的 60 对(dedup_pairs.json);人工标注:dup(合并不丢任何建议)/
not(建议不同、互补、条件不同或方向相反——即使主题相关)/ borderline(不计分)。

  TYPESAFE_API_KEY=... python3 tests/eval/jev_gate/run_dedup.py
"""

from __future__ import annotations

import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_jev_gate import EVAL, HERE, MODEL, OUT, _load_cache, call_jev  # noqa: E402

DUP_Q = "`规则A` 和 `规则B` 是否给出了实质相同的建议——在相同情境下,照其中任何一条做,效果都一样?"
DUP_CRIT = {"true": "两条讲的是同一件事,合并成一条不会丢失任何建议",
            "false": "建议不同、互补、适用条件不同或方向相反(即使主题相关)"}


def rule(e: dict) -> dict:
    return {"触发": ";".join(p["value"] for p in e.get("patterns", [])), "建议": e["then"],
            "依据": e.get("because", "")}


def legacy_merge(a: dict, b: dict) -> bool:
    """app/learning/consolidation.merge_similar 的判定(同领域 + 触发词精确重叠 + 空格分词重叠)。"""
    if a.get("domain") != b.get("domain"):
        return False
    kw_a = {p["value"].lower() for p in a.get("patterns", [])}
    kw_b = {p["value"].lower() for p in b.get("patterns", [])}
    if not kw_a or not kw_b or len(kw_a & kw_b) / min(len(kw_a), len(kw_b)) < 0.8:
        return False
    wa = {w.lower() for w in a["then"].split() if len(w) > 2}
    wb = {w.lower() for w in b["then"].split() if len(w) > 2}
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.4


def main() -> None:
    key = os.environ["TYPESAFE_API_KEY"]
    corpus = {e["id"]: e for f in glob.glob(str(EVAL / "corpus" / "*.json")) for e in json.load(open(f))}
    pairs = json.load((HERE / "dedup_pairs.json").open(encoding="utf-8"))["pairs"]
    cache = _load_cache()

    def one(pr):
        a, b = corpus[pr["a"]], corpus[pr["b"]]
        payload = {"model": MODEL, "state": {"规则A": rule(a), "规则B": rule(b)},
                   "questions": {"dup": {"type": "noul", "instructions": DUP_Q, "criteria": DUP_CRIT}}}
        p = call_jev(payload, cache, key)["response"]["answers"]["dup"]["noul"]
        return pr, p, legacy_merge(a, b)

    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(one, pairs))
    scored = [(pr["label"], p, lg) for pr, p, lg in rows if pr["label"] != "borderline"]
    n_dup = sum(lbl == "dup" for lbl, _, _ in scored)
    print(f"pairs={len(rows)} scored={len(scored)} dup={n_dup}")
    lg_tp = sum(lg and lbl == "dup" for lbl, _, lg in scored)
    lg_fp = sum(lg and lbl == "not" for lbl, _, lg in scored)
    print(f"legacy merge_similar: merges={lg_tp + lg_fp} TP={lg_tp} FP={lg_fp} recall={lg_tp / n_dup:.2f}")
    for tau in (0.5, 0.7, 0.8, 0.9, 0.95):
        tp = sum(p >= tau and lbl == "dup" for lbl, p, _ in scored)
        fp = sum(p >= tau and lbl == "not" for lbl, p, _ in scored)
        prec = tp / (tp + fp) if tp + fp else 1.0
        print(f"jev τ={tau}: merges={tp + fp} TP={tp} FP={fp} precision={prec:.2f} recall={tp / n_dup:.2f}")
    for pr, p, _ in sorted(rows, key=lambda r: -r[1]):
        if (pr["label"] == "not" and p >= 0.5) or (pr["label"] == "dup" and p < 0.8):
            print(f"  {pr['label']:10s} p={p:.2f} {pr['a']} ~ {pr['b']}")
    (OUT / "dedup_results.json").write_text(json.dumps(
        [{**pr, "jev_p": p, "legacy": lg} for pr, p, lg in rows], ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
