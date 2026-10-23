"""判定记录:每次 prime 当时的完整输入与决定,事后结果另表回填。

一条记录(decision_log)存:
- request:原样发给判官的 /v1/systemone 请求(state + 带类型的问题,含规则原文、过往情境提示、
  类比题)——任何老师(Jev / Laya / 大模型)都能直接重放作答;
- answers:判官原始作答;policy:当时是谁、用什么问法和门槛做的决定(on-policy 必需);
- candidates:判定池里每条记忆的 id、版本与上游信号(召回名次、精排分、经验相似度、票数、
  是否词面命中、是否救援)和判官概率;injected:最终推送及入选路径(主选/补充/类比……)。

事后结果(decision_outcomes)按语义在各处记录:report 成功/失败、被动评估 helped/contradicted、
capture 发现"本该想起却没推"(revealed)、评测的标准答案(gold)。能直接带判定 id 的带上,
否则导出时按 (会话, 情境哈希) → (会话, 记忆) 的顺序找回对应判定。

运行参数 distill.decision_log(默认开)控制;长期保存靠归档(app/distill/archive.py),库里只删已归档
且超过 distill.keep_days(默认 90)天的行。
"""

from __future__ import annotations

import hashlib

from app import settings
from app.learning.privacy import scrub_secrets
from app.matching.lexical import lexical_on
from app.matching.selection import analogy_mode, thresholds


def decision_log_on() -> bool:
    return settings.get("distill.decision_log")


def shadow_judges(source: str = "prime") -> list[str]:
    """distill.shadow_judges(CE_SHADOW_JUDGES):模型注册名列表,每次判定后在后台原样作答并记入
    teacher_labels,不影响决定——候选学生上岗前的影子期,也是 on-policy 的老师标注。
    distill.shadow_rate(0~1,默认 1)按比例抽样,贵的大模型老师可以只抽一部分;评测来源不跑影子。"""
    import random
    names = settings.get("distill.shadow_judges")
    if not names or source in ("eval", "stream"):
        return []
    return names if random.random() < settings.get("distill.shadow_rate") else []


def keep_days() -> int:
    return settings.get("distill.keep_days")


def context_hash(text: str) -> str:
    """情境指纹(脱敏、折叠空白后取哈希):capture 挑出的会话情境据此找回当时的判定。"""
    norm = " ".join(scrub_secrets(text or "").split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:20]


def _policy(trace: dict) -> dict:
    return {
        "judge": ("llm_fallback" if trace.get("judge") == "llm_fallback"
                  else "jev" if trace.get("jev_status") == "ok"
                  else "fallback" if trace.get("jev_status") in ("failed", "timeout")
                  else "dosage"),
        "model": trace.get("jev_model", ""),
        "jev_status": trace.get("jev_status", "off"),
        "jev_mode": trace.get("jev_mode", "single"),
        "jev_calls": trace.get("jev_calls", 0),
        "prompt": settings.get("judge.prompt"),
        "stage": settings.get("judge.stage"),
        "analogy": analogy_mode(),
        "lexical": "on" if lexical_on() else "off",
        # 生效的选择阈值(不只是被覆盖的环境变量):默认值会随版本调整,训练时要知道当时按什么规则注入
        "rule": thresholds(),
    }


def build_record(decision_id: str, context: str, trace: dict, results: list, *,
                 session_id: str = "", project: str = "", domain: str = "") -> dict:
    """由 pipeline 的 trace 与最终结果组装一条判定记录(纯函数,不做 I/O)。"""
    recalled = {eid: i + 1 for i, eid in enumerate(trace.get("recalled_ids", []))}
    rerank = dict(trace.get("rerank_raw", []))
    tier = {eid: t for eid, _, t in trace.get("scored", [])}
    floor_ok = set(tier)
    jev = {eid: (p, q) for eid, p, q in trace.get("jev", [])}
    analogy = {eid: (ap, aq) for eid, ap, aq in trace.get("analogy", [])}
    hints = trace.get("hint_scores", {})
    votes = trace.get("exemplar_votes", {})
    lexical = set(trace.get("lexical_ids", []))
    pool = trace.get("jev_pool") or [[eid, None] for eid in jev]
    candidates = []
    for i, (eid, version) in enumerate(pool):
        p, q = jev.get(eid, (None, None))
        ap, aq = analogy.get(eid, (None, None))
        candidates.append({
            "id": eid, "version": version, "key": f"c{i}",
            "recall_rank": recalled.get(eid), "rerank": rerank.get(eid), "tier": tier.get(eid),
            "rescued": eid not in floor_ok, "hint": hints.get(eid), "votes": votes.get(eid, 0),
            "lexical": eid in lexical, "p": p, "q": q, "analog_p": ap, "analog_q": aq,
        })
    request = trace.get("jev_request")
    supplement_request = trace.get("jev_supplement_request") or {}
    if request and supplement_request:
        request = {"state": request.get("state", ""),
                   "questions": {**(request.get("questions") or {}),
                                  **(supplement_request.get("questions") or {})}}
    answers = trace.get("jev_answers")
    supplement_answers = trace.get("jev_supplement_answers") or {}
    if answers and supplement_answers:
        answers = {**answers, **supplement_answers}
    return {
        "id": decision_id,
        "session_id": session_id or "",
        "project": project or "",
        "domain": domain or "",
        "context_hash": context_hash(context),
        "policy": _policy(trace),
        "request": ({"state": scrub_secrets(str(request.get("state", ""))), "questions": request.get("questions", {})}
                    if request else {"state": scrub_secrets(context), "questions": {}}),
        "answers": answers,
        "candidates": candidates,
        "injected": [{"id": r.enzyme.id, "reason": getattr(r, "reason", ""), "exploratory": bool(r.exploratory)}
                     for r in results],
        # 类比题 a{k} 对应的记忆(顺序与请求里的类比题一致)
        "analogy": [{"key": f"a{k}", "id": eid} for k, (eid, _, _) in enumerate(trace.get("analogy", []))],
    }
