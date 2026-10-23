"""System 1 网关:给其他 agent 用的 /v1/systemone(TypeSafe 的 System One API 协议,Jev / laya-serve 同款)。

按路由表依次问判官(运行参数 gateway.route,默认 ["jev"];例如 laya-local → glm-5.3 → jev):前面的判官
对每道题都够有把握就直接返回,否则升级给下一位(最后一位的答案无论如何都返回)。请求里指定
model 时只问那一位。每次调用都写进判定记录(来源 gateway),路上每位判官的作答都存为老师标注——
别的 agent 用得越多,自训判官的数据就越多;调用方可以回传真实结果(/v1/systemone/feedback)。

"有把握":noul 概率 ≤ gateway.low(0.15)或 ≥ gateway.high(0.85);choice 最高项 ≥
gateway.choice(0.6);score 题不设门槛。
"""

from __future__ import annotations

import json
import logging
import time

from app import model_registry, settings
from app.distill.records import context_hash, decision_log_on
from app.learning.privacy import scrub_secrets

logger = logging.getLogger(__name__)


class NoJudgeAvailable(RuntimeError):
    pass


def route() -> list[str]:
    return settings.get("gateway.route")


def confident(answers: dict, questions: dict) -> bool:
    lo, hi, cmin = settings.get("gateway.low"), settings.get("gateway.high"), settings.get("gateway.choice")
    for qid, q in questions.items():
        a = answers.get(qid)
        if a is None:
            return False
        if q.get("type") == "choice":
            probs = a.get("probabilities") or {}
            if not probs or max(probs.values()) < cmin:
                return False
        elif q.get("type", "noul") == "noul":
            p = a.get("noul")
            if p is None or lo < p < hi:
                return False
    return True


_TEACHERS: dict = {}
model_registry.on_change(lambda: _TEACHERS.clear())   # 后台改了模型 → 下次按新配置实例化


def _teacher(name: str):
    if name not in _TEACHERS:
        from app.distill.teachers import load_teachers
        _TEACHERS[name] = load_teachers([name])[name]
    return _TEACHERS[name]


async def answer(state, questions: dict, model: str = "") -> dict:
    """→ {"model": 最终作答的判官, "answers": ..., "path": [...], "tried": {判官: 作答}}。"""
    names = [model] if model else route()
    path, tried = [], {}
    for i, name in enumerate(names):
        t0 = time.perf_counter()
        try:
            ans = await _teacher(name).answer(state, questions)
        except KeyError:
            path.append({"judge": name, "ok": False, "error": "unknown judge"})
            continue
        ok = ans.answers is not None
        conf = ok and confident(ans.answers, questions)
        path.append({"judge": name, "ok": ok, "confident": conf, "latency_s": round(time.perf_counter() - t0, 3)})
        if not ok:
            continue
        tried[name] = ans
        if conf or i == len(names) - 1:
            return {"model": name, "answers": ans.answers, "path": path, "tried": tried}
    if tried:   # 最后一位失败:退回最后一个成功的
        name = list(tried)[-1]
        return {"model": name, "answers": tried[name].answers, "path": path, "tried": tried}
    raise NoJudgeAvailable(f"no judge answered: {path}")


async def record(pg, decision_id: str, state, questions: dict, result: dict, agent: str = "") -> None:
    """写判定记录(来源 gateway)+ 路上每位判官的作答(老师标注)。best-effort,调用方放后台。"""
    if not decision_log_on():
        return
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    try:
        await pg.insert_decision({
            "id": decision_id, "session_id": "", "project": agent or "", "domain": "",
            "context_hash": context_hash(text),
            "policy": {"source": "gateway", "judge": result["model"], "agent": agent, "path": result["path"]},
            "request": {"state": scrub_secrets(text), "questions": questions},
            "answers": result["answers"], "candidates": [], "injected": [],
        })
        for name, ans in result.get("tried", {}).items():
            await pg.upsert_teacher_label(decision_id, name, ans.answers, ans.usage)
    except Exception:
        logger.debug("gateway decision log failed (non-fatal)", exc_info=True)


async def serve(engine, state, questions: dict, model: str = "", agent: str = "") -> dict:
    """REST(/api/v1/systemone)与 MCP(system_one 工具)共用:作答 + 后台记录,返回给调用方的结果。"""
    import uuid
    result = await answer(state, questions, model=model)
    decision_id = uuid.uuid4().hex
    engine.spawn(record(engine.pg, decision_id, state, questions, result, agent=agent))
    return {"model": result["model"], "answers": result["answers"], "decision_id": decision_id,
            "routing": {"path": result["path"]}}


async def feedback(engine, decision_id: str, labels: dict, source: str = "agent") -> int:
    """回传真实结果:noul 题给 true/false,choice 题给正确选项的键。"""
    for qid, val in labels.items():
        outcome = ("true" if val else "false") if isinstance(val, bool) else f"choice:{val}"
        await engine.record_outcome(qid, outcome, f"gateway:{source}", decision_id=decision_id)
    return len(labels)

