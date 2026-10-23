"""上岗门槛:候选判官(自训的 Laya / Jev / 小模型……)在固定考场上和现任判官比,不输才给上岗建议。

考场是带真实答案的判定集(arena 格式:评测 / 经验流的判定导出,或擂台还原的静态基准)。两位判官
对同一批判定作答(带缓存),比较:选择 F1(召回与精度)、静默率、逐条判断的 AUC。候选在每一项都
不低于现任减去容差才算通过。结果写进判官登记表(CE_JUDGE_REGISTRY,默认 data/distill/registry.json),
不会自动切换线上判官——通过后建议先把候选加进影子判官(运行参数 distill.shadow_judges)在线上观察,
再在后台「模型绑定」里把适用性判官(或 System 1 网关路由 gateway.route)指向它。

  python -m app.distill.gate --candidate laya-local --incumbent jev --source jsonl:data/exam.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path


def registry_path() -> Path:
    return Path(os.environ.get("CE_JUDGE_REGISTRY", "data/distill/registry.json"))


def _f1(m: dict) -> float:
    r, p = m.get("sel_recall") or 0.0, m.get("sel_precision") or 0.0
    return 2 * r * p / (r + p) if r + p else 0.0


def verdict(cand: dict, inc: dict, tol_f1: float = 0.0, tol_silence: float = 0.0, tol_auc: float = 0.01) -> dict:
    """逐项比较(整体指标);缺失的项不参与。"""
    checks = {
        "selection_f1": (_f1(cand), _f1(inc), tol_f1),
        "silence": (cand.get("sel_silence"), inc.get("sel_silence"), tol_silence),
        "noul_auc": (cand.get("noul_auc"), inc.get("noul_auc"), tol_auc),
    }
    detail, ok = {}, True
    for name, (c, i, tol) in checks.items():
        if c is None or i is None:
            continue
        passed = c >= i - tol
        ok &= passed
        detail[name] = {"candidate": round(c, 4), "incumbent": round(i, 4), "tolerance": tol, "passed": passed}
    return {"passed": ok and bool(detail), "checks": detail}


def record(name: str, incumbent: str, source: str, result: dict, metrics: dict) -> dict:
    path = registry_path()
    reg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"judges": {}}
    entry = reg["judges"].setdefault(name, {"status": "candidate", "history": []})
    entry["history"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "vs": incumbent, "exam": source,
                             "passed": result["passed"], "checks": result["checks"], "metrics": metrics})
    entry["status"] = "passed" if result["passed"] else "failed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return entry


async def run(candidate: str, incumbent: str, source: str, cache_root: Path, limit: int = 0) -> dict:
    from app.distill.arena import decisions_from_jsonl, metrics, run_teacher
    from app.distill.teachers import load_teachers
    decisions = decisions_from_jsonl(source.partition(":")[2] if ":" in source else source)
    decisions = [d for d in decisions if d.get("kind") in ("positive", "silent")]
    if limit:
        decisions = decisions[:limit]
    answers = {}
    for name in (candidate, incumbent):
        logged = {d["id"]: d["logged"][name] for d in decisions if name in d.get("logged", {})}
        if len(logged) == len(decisions):          # 考场里已经有这位判官的作答(如线上记录的 Jev)
            answers[name] = logged
        else:
            teacher = load_teachers([name])[name]
            answers[name] = await run_teacher(name, teacher, decisions, cache_root)
    m = {name: metrics(decisions, answers[name]) for name in answers}
    result = verdict(m[candidate], m[incumbent])
    entry = record(candidate, incumbent, source, result, m)
    return {"exam_size": len(decisions), **result, "status": entry["status"], "metrics": m}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--incumbent", default="jev")
    ap.add_argument("--source", required=True, help="jsonl:<arena 格式、带真实答案的判定>")
    ap.add_argument("--cache", default="data/distill/cache")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    async def _go():
        from app.runtime import cli_admin_state
        await cli_admin_state()          # 连得上库就用后台的模型配置,否则只看环境变量
        return await run(args.candidate, args.incumbent, args.source, Path(args.cache), args.limit)

    out = asyncio.run(_go())
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out["passed"]:
        print(f"通过:建议先在后台「参数配置 → 判定记录 → 影子判官」加入 {args.candidate},"
              "观察线上影子数据,再在「模型绑定」里切换适用性判官。")
    else:
        print("未通过:保持现任判官。")


if __name__ == "__main__":
    main()
