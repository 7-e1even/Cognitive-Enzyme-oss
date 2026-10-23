"""老师校准与多老师合成:让不同老师的概率可比、可信,再合成一份软标签。

大模型直接写出的概率常常过于自信或过于保守,Jev 也有自己的偏差。用带真实答案的判定
(评测 / 经验流的标准答案、线上回填的结果)为每位老师拟合:
- noul:Platt 缩放 p' = σ(a·logit(p) + b);
- choice:温度 T,q'_k ∝ q_k^(1/T)(含 none)。
参数存在 CE_CALIBRATION_FILE(默认 data/distill/calibration.json),按老师名索引。

合成(combine):各老师先校准,再加权——noul 在 logit 空间取加权平均,choice 取加权几何平均
(对数线性汇聚)后归一。分歧度(disagreement)高的题是难例,值得换更强的老师或人工看。

  python -m app.distill.calibrate --source jsonl:<arena 导出> --teachers glm-5.3,jev-1.13.0 \\
      [--answers-dir tests/eval/distill/data/cache]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def calibration_path() -> Path:
    return Path(os.environ.get("CE_CALIBRATION_FILE", "data/distill/calibration.json"))


def load_params() -> dict:
    p = calibration_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fit_platt(pairs: list[tuple[float, int]], iters: int = 500, lr: float = 0.5) -> dict:
    """noul:最小化对数损失拟合 (a, b)。样本太少时返回恒等变换。"""
    if len(pairs) < 20 or len({y for _, y in pairs}) < 2:
        return {"a": 1.0, "b": 0.0}
    a, b = 1.0, 0.0
    xs = [(_logit(p), y) for p, y in pairs]
    n = len(xs)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in xs:
            d = _sigmoid(a * x + b) - y
            ga += d * x
            gb += d
        a -= lr * ga / n
        b -= lr * gb / n
    return {"a": round(a, 4), "b": round(b, 4)}


def _temper(probs: dict[str, float], t: float) -> dict[str, float]:
    logs = {k: math.log(max(v, 1e-6)) / t for k, v in probs.items()}
    m = max(logs.values())
    ex = {k: math.exp(v - m) for k, v in logs.items()}
    s = sum(ex.values())
    return {k: v / s for k, v in ex.items()}


def fit_temperature(items: list[tuple[dict[str, float], str]]) -> dict:
    """choice:在一组候选温度里取对数损失最小的。"""
    items = [(pr, g) for pr, g in items if pr and g in pr]
    if len(items) < 10:
        return {"T": 1.0}
    grid = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0]

    def loss(t):
        return -sum(math.log(max(_temper(pr, t)[g], 1e-9)) for pr, g in items) / len(items)
    return {"T": min(grid, key=loss)}


def apply(answers: dict, params: dict | None) -> dict:
    """按老师的校准参数变换一份作答(System One API 的 answers 形状);没有参数原样返回。"""
    if not params or not answers:
        return answers
    a, b = params.get("noul", {}).get("a", 1.0), params.get("noul", {}).get("b", 0.0)
    t = params.get("choice", {}).get("T", 1.0)
    out = {}
    for qid, ans in answers.items():
        if not isinstance(ans, dict):
            continue
        if "noul" in ans and ans["noul"] is not None:
            out[qid] = {**ans, "noul": _sigmoid(a * _logit(float(ans["noul"])) + b)}
        elif ans.get("probabilities"):
            probs = _temper({k: float(v) for k, v in ans["probabilities"].items()}, t)
            out[qid] = {**ans, "probabilities": probs, "choice": max(probs, key=probs.get)}
        else:
            out[qid] = ans
    return out


def combine(answer_sets: dict[str, dict], weights: dict[str, float] | None = None) -> dict:
    """多老师合成(各自先校准后再传进来):noul 取 logit 加权平均,choice 取加权几何平均。"""
    weights = weights or {}
    qids = {q for ans in answer_sets.values() for q in (ans or {})}
    out = {}
    for qid in qids:
        nouls, choices = [], []
        for name, ans in answer_sets.items():
            a = (ans or {}).get(qid)
            w = weights.get(name, 1.0)
            if not isinstance(a, dict) or w <= 0:
                continue
            if a.get("noul") is not None:
                nouls.append((w, float(a["noul"])))
            elif a.get("probabilities"):
                choices.append((w, a["probabilities"]))
        if nouls:
            tw = sum(w for w, _ in nouls)
            out[qid] = {"type": "noul", "noul": _sigmoid(sum(w * _logit(p) for w, p in nouls) / tw)}
        elif choices:
            keys = set().union(*(pr.keys() for _, pr in choices))
            tw = sum(w for w, _ in choices)
            logs = {k: sum(w * math.log(max(float(pr.get(k, 0.0)), 1e-6)) for w, pr in choices) / tw for k in keys}
            m = max(logs.values())
            ex = {k: math.exp(v - m) for k, v in logs.items()}
            s = sum(ex.values())
            probs = {k: v / s for k, v in ex.items()}
            out[qid] = {"type": "choice", "probabilities": probs, "choice": max(probs, key=probs.get)}
    return out


def disagreement(answer_sets: dict[str, dict], qid: str) -> float:
    """老师之间的最大分歧:noul 取概率差,choice 取总变差距离。"""
    vals = [(a or {}).get(qid) for a in answer_sets.values()]
    vals = [v for v in vals if isinstance(v, dict)]
    worst = 0.0
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            x, y = vals[i], vals[j]
            if x.get("noul") is not None and y.get("noul") is not None:
                worst = max(worst, abs(float(x["noul"]) - float(y["noul"])))
            elif x.get("probabilities") and y.get("probabilities"):
                keys = set(x["probabilities"]) | set(y["probabilities"])
                worst = max(worst, 0.5 * sum(abs(float(x["probabilities"].get(k, 0)) - float(y["probabilities"].get(k, 0)))
                                             for k in keys))
    return worst


def fit_teacher(decisions: list[dict], answers: dict[str, dict]) -> dict:
    """用带真实答案的判定(arena 格式)拟合一位老师的校准参数。"""
    from app.distill.arena import _ece
    nouls, choices = [], []
    for d in decisions:
        ans = answers.get(d["id"])
        if not ans or d.get("kind") not in ("positive", "silent"):
            continue
        pos = set(d.get("positive", []))
        for i in range(len(d.get("candidates", []))):
            a = ans.get(f"c{i}")
            if isinstance(a, dict) and a.get("noul") is not None:
                nouls.append((float(a["noul"]), 1 if f"c{i}" in pos else 0))
        probs = (ans.get("best") or {}).get("probabilities")
        if probs:
            gold = "none" if d["kind"] == "silent" else next((k for k in probs if k in pos), "")
            if gold:
                choices.append(({k: float(v) for k, v in probs.items()}, gold))
    params = {"noul": fit_platt(nouls), "choice": fit_temperature(choices),
              "n_noul": len(nouls), "n_choice": len(choices), "fitted_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    a, b = params["noul"]["a"], params["noul"]["b"]
    params["ece_before"] = round(_ece(nouls), 4) if nouls else None
    params["ece_after"] = round(_ece([(_sigmoid(a * _logit(p) + b), y) for p, y in nouls]), 4) if nouls else None
    return params


def main() -> None:
    from app.distill.arena import decisions_from_jsonl
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="jsonl:<arena 格式的判定>")
    ap.add_argument("--teachers", required=True, help="逗号分隔;作答取判定里记下的(logged)或 --answers-dir 的缓存")
    ap.add_argument("--answers-dir", default="", help="擂台缓存目录(<dir>/<老师>/<题面哈希>.json)")
    args = ap.parse_args()
    decisions = decisions_from_jsonl(args.source.partition(":")[2])
    params = load_params()
    for name in [t.strip() for t in args.teachers.split(",") if t.strip()]:
        answers = {d["id"]: d["logged"][name] for d in decisions if name in d.get("logged", {})}
        if args.answers_dir:
            from app.distill.arena import _hash
            for d in decisions:
                f = Path(args.answers_dir) / name / f"{_hash(d)}.json"
                if f.exists():
                    answers[d["id"]] = json.loads(f.read_text(encoding="utf-8"))["answers"]
        params[name] = fit_teacher(decisions, answers)
        print(name, json.dumps(params[name], ensure_ascii=False))
    path = calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"saved → {path}")


if __name__ == "__main__":
    main()
