"""判定记录导出:同一份原始记录 → 训练自己的判官(Laya)或交给其他大模型用的多种格式。

  python -m app.distill.export --format laya --out data/laya.jsonl [--since 2026-09-01] [--source prime,stream]
  python -m app.distill.export --format kd --out kd.jsonl --archive data/distill/archive --no-db   # 训练机上只读归档

格式:
  arena      老师擂台的 decision 格式(tests/eval/distill/arena.py --source jsonl:<path>)
  laya       与 HuggingFace LocalLLaMA/typed-decisions 同构(Laya 官方微调 notebook 直接可读):
             真实结果作硬标签、老师概率作软标签,单选题截成短名单(Laya 的选项预算有限)
  kd         生成式小模型(Qwen 等)的逐题训练样本:选项用单个字母,target 为字母上的目标分布
  sft        对话格式:情境 + 编号候选 → 该推哪几条(有真实标签用标签,否则用老师的判定)
  pairs      偏好对:同一情境下的正确记忆 vs 最像对的错误记忆(DPO / 奖励模型)
  retrieval  检索三元组:情境、正确记忆、难负例(训练向量模型 / 精排模型)

标签:
- 评测/经验流的判定挂了标准答案(gold / acceptable / silent),视为"闭世界":候选里没标的都是负例;
- 线上判定只有部分反馈(helped / success / revealed 为正,contradicted / failure 为负),其余未知。
事后结果先按判定 id 对上;没有 id 的按 (会话, 情境哈希)、再按 (会话, 记忆) 找回当时那次判定。

数据只来自本机 PG;交给外部模型之前,由部署方确认隐私与服务条款。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.matching.selection import select

logger = logging.getLogger(__name__)

POSITIVE = {"gold", "acceptable", "helped", "success", "revealed"}
NEGATIVE = {"contradicted", "failure"}
CLOSED = {"gold", "silent"}           # 带这类结果的判定,标签是完整的

SFT_SYSTEM = ("你是记忆注入判官。给你 agent 当前的情境和若干条候选记忆,选出对处理这个情境直接有用、"
              "应该提醒 agent 的记忆;都没用就一条都不选。只输出 JSON:{\"inject\": [候选编号, ...]}。")


_CALIB: dict | None = None


def _calibration() -> dict:
    global _CALIB
    if _CALIB is None:
        from app.distill.calibrate import load_params
        _CALIB = load_params()
    return _CALIB


def rule_text(rule) -> str:
    if isinstance(rule, dict):
        return " | ".join(f"{k}: {v}" for k, v in rule.items() if v)
    return str(rule or "")


def _cand_rules(rec: dict) -> dict[str, str]:
    qs = (rec.get("request") or {}).get("questions") or {}
    choices = (qs.get("best") or {}).get("criteria") or {}
    return {c["key"]: rule_text((qs.get(c["key"], {}).get("instructions") or {}).get("rule")
                                or choices.get(c["key"]))
            for c in rec.get("candidates", [])}


def resolve_outcomes(decisions: list[dict], outcomes: list[dict]) -> dict[str, list[dict]]:
    """把事后结果挂到判定上:判定 id → (会话, 情境哈希) → (会话, 记忆),找不到的丢弃。"""
    by_id = {d["id"]: d for d in decisions}
    by_session = defaultdict(list)
    for d in decisions:
        if d.get("session_id"):
            by_session[d["session_id"]].append(d)
    for lst in by_session.values():
        lst.sort(key=lambda d: d["created_at"])
    out: dict[str, list[dict]] = defaultdict(list)
    for o in outcomes:
        target = by_id.get(o.get("decision_id") or "")
        if target is None and o.get("session_id"):
            cands = [d for d in by_session.get(o["session_id"], []) if d["created_at"] <= o["created_at"]]
            if o.get("context_hash"):
                same = [d for d in cands if d["context_hash"] == o["context_hash"]]
                target = same[-1] if same else None
            if target is None:
                eid = o["enzyme_id"]
                with_e = [d for d in cands if any(c["id"] == eid for c in d.get("candidates", []))
                          or any(i["id"] == eid for i in d.get("injected", []))]
                target = with_e[-1] if with_e else None
        if target is not None:
            out[target["id"]].append(o)
    return out


def question_labels(rec: dict, outs: list[dict]) -> tuple[dict[str, int], dict[str, str]]:
    """网关判定的真实结果按题号回传:noul → true/false,choice → choice:<选项>。"""
    qids = set((rec.get("request") or {}).get("questions") or {})
    noul, choice = {}, {}
    for o in outs:
        qid, oc = o["enzyme_id"], o["outcome"]
        if qid not in qids:
            continue
        if oc in ("true", "false"):
            noul[qid] = 1 if oc == "true" else 0
        elif oc.startswith("choice:"):
            choice[qid] = oc.split(":", 1)[1]
    return noul, choice


def labels_for(rec: dict, outs: list[dict]) -> tuple[dict[str, int], str, bool]:
    """→ ({候选键 / 题号: 1/0}, 单选目标 "c3"/"none"/"", 是否闭世界)。"""
    key_of = {c["id"]: c["key"] for c in rec.get("candidates", [])}
    closed = any(o["outcome"] in CLOSED for o in outs)
    labels: dict[str, int] = {}
    if closed:
        labels = {k: 0 for k in key_of.values()}
    labels.update(question_labels(rec, outs)[0])
    for o in outs:
        k = key_of.get(o["enzyme_id"])
        if k is None:
            continue
        if o["outcome"] in POSITIVE:
            labels[k] = 1
        elif o["outcome"] in NEGATIVE and labels.get(k) != 1:
            labels[k] = 0
    pos = [k for k, v in labels.items() if v == 1]
    gold_keys = [key_of[o["enzyme_id"]] for o in outs if o["outcome"] == "gold" and o["enzyme_id"] in key_of]
    if gold_keys:
        best = gold_keys[0]
    elif pos:
        best = pos[0]
    elif any(o["outcome"] == "silent" for o in outs) or (closed and not pos):
        best = "none"
    else:
        best = ""
    return labels, best, closed


def teacher_select(answers: dict, n: int, rescued: set[str] = frozenset()) -> list[str]:
    """没有真实标签时,用老师的概率按线上核心规则选(app/matching/selection.py,与 pipeline 同源)。"""
    p = {f"c{i}": (answers.get(f"c{i}") or {}).get("noul", 0.0) for i in range(n)}
    probs = (answers.get("best") or {}).get("probabilities", {})
    return select(p, {k: probs.get(k, 0.0) for k in p}, probs.get("none", 0.0), rescued)


def _norm(d: dict) -> dict:
    total = sum(d.values())
    return {k: v / total for k, v in d.items()} if total > 0 else {k: 1.0 / len(d) for k in d}


def laya_rows(rec: dict, qs: dict, state: str, labels: dict, best: str, closed: bool, soft: dict,
              prefer: list[str], meta: dict, max_options: int, mix: float, outs: list[dict] = ()) -> list[dict]:
    """Laya 微调格式(与 HuggingFace LocalLLaMA/typed-decisions 同构,官方 notebook 直接可读):
    state / questions / gold 均为 JSON 字符串;gold[qid] = {label, probabilities}。
    有真实标签的题用真实标签(mix>0 时按比例混入老师概率),没有的用老师概率(软标签);
    都没有的题不导出。单选选项多于 max_options 时截成"正确答案 + 老师最看好的"短名单。"""
    teacher = next((t for t in prefer if t in soft), next(iter(soft), ""))
    tans = soft.get(teacher) or {}
    alabels = {}
    for a in rec.get("analogy", []):
        k = next((c["key"] for c in rec.get("candidates", []) if c["id"] == a["id"]), None)
        if k in labels:
            alabels[a["key"]] = labels[k]
    abest = next((a["key"] for a in rec.get("analogy", []) if alabels.get(a["key"]) == 1),
                 "none" if closed and rec.get("analogy") else "")
    choice_gold = question_labels(rec, outs)[1]
    out_q, gold = {}, {}
    for qid, q in qs.items():
        if q.get("type") == "choice":
            crit = q.get("criteria") or {}
            opts = list(crit)
            tp = (tans.get(qid) or {}).get("probabilities") or {}
            hard_best = choice_gold.get(qid) or (best if qid == "best" else (abest if qid == "same" else ""))
            if hard_best in opts:
                probs = {k: (1 - mix) * (1.0 if k == hard_best else 0.0) + mix * tp.get(k, 0.0) for k in opts}
            elif tp:
                probs = {k: tp.get(k, 0.0) for k in opts}
            else:
                continue
            real = [k for k in opts if k != "none"]
            if max_options and len(real) > max_options:
                keep = [k for k in real if k == hard_best]
                keep += [k for k in sorted(real, key=lambda k: -(tp.get(k, probs[k]))) if k not in keep]
                keep = set(keep[:max_options])
                opts = [k for k in opts if k in keep or k == "none"]
                crit = {k: crit[k] for k in opts}
            probs = _norm({k: probs[k] for k in opts})
            out_q[qid] = {**q, "criteria": crit}
            gold[qid] = {"label": max(probs, key=probs.get), "probabilities": probs}
        else:
            hard = labels.get(qid, alabels.get(qid))
            tval = (tans.get(qid) or {}).get("noul")
            if hard is not None:
                pv = (1 - mix) * hard + mix * (tval if tval is not None else hard)
            elif tval is not None:
                pv = tval
            else:
                continue
            out_q[qid] = q
            gold[qid] = {"label": "true" if pv >= 0.5 else "false", "probabilities": {"true": pv, "false": 1 - pv}}
    if not gold:
        return []
    return [{"id": rec["id"], "workflow": meta.get("domain") or meta.get("source") or "prime",
             "state": json.dumps(state, ensure_ascii=False), "questions": json.dumps(out_q, ensure_ascii=False),
             "gold": json.dumps(gold, ensure_ascii=False), "meta": {**meta, "soft_teacher": teacher}}]


KD_SYSTEM = "你是快速判定模型。读完情境和问题,只输出一个选项字母,不要任何其他文字。"
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def kd_rows(laya_row: dict) -> list[dict]:
    """生成式学生(Qwen 等)的训练样本:每道题一条,选项用单个字母(一个 token),
    target 是字母上的目标分布(真实标签 / 老师概率),可同时做 SFT(label)与逐 token 的 KL 拟合。"""
    state = json.loads(laya_row["state"])
    questions, gold = json.loads(laya_row["questions"]), json.loads(laya_row["gold"])
    rows = []
    for qid, q in questions.items():
        g = gold.get(qid)
        if g is None:
            continue
        ins = q.get("instructions")
        if isinstance(ins, dict):
            text = "\n".join([str(ins.get("question", ""))] +
                              [f"{k}: {rule_text(v)}" for k, v in ins.items() if k != "question"])
        else:
            text = str(ins or "")
        if q.get("type") == "choice":
            keys = list(g["probabilities"])
            if len(keys) > len(_LETTERS):
                # 选项超 26 个(--max-options 0 不截断时)转不下字母卷,跳过该题
                logger.warning("kd_rows: question %s has %d options (>26), skipped", qid, len(keys))
                continue
            letters = dict(zip(keys, _LETTERS))
            crit = q.get("criteria") or {}
            opts = "\n".join(f"{letters[k]}) {rule_text(crit.get(k, k))}" for k in keys)
            target = {letters[k]: v for k, v in g["probabilities"].items()}
            label = letters[g["label"]]
        else:
            crit = q.get("criteria") or {}
            opts = ("A) 是" + (f"——{crit.get('true')}" if isinstance(crit, dict) and crit.get("true") else "")
                    + "\nB) 否" + (f"——{crit.get('false')}" if isinstance(crit, dict) and crit.get("false") else ""))
            target = {"A": g["probabilities"]["true"], "B": g["probabilities"]["false"]}
            label = "A" if g["label"] == "true" else "B"
        user = f"# 情境\n{state}\n\n# 问题\n{text}\n\n# 选项\n{opts}"
        rows.append({"messages": [{"role": "system", "content": KD_SYSTEM}, {"role": "user", "content": user},
                                  {"role": "assistant", "content": label}],
                     "options": list(target), "target": target, "label": label,
                     "meta": {**laya_row["meta"], "qid": qid, "type": q.get("type", "noul")}})
    return rows


def to_formats(rec: dict, outs: list[dict], teachers: dict[str, dict], fmt: str,
               prefer: list[str], max_options: int = 8, mix: float = 0.0) -> list[dict]:
    req = rec.get("request") or {}
    qs = req.get("questions") or {}
    if not qs:
        return []
    state = req.get("state", "")
    cands = rec.get("candidates", [])
    labels, best, closed = labels_for(rec, outs)
    judge = (rec.get("policy") or {}).get("model") or "judge"
    soft = {**({judge: rec["answers"]} if rec.get("answers") else {}), **teachers}
    if "ensemble" in prefer and soft:
        # 多老师合成:各自按真实答案校准后加权(app/distill/calibrate.py)
        from app.distill.calibrate import apply, combine
        params = _calibration()
        soft = {**soft, "ensemble": combine({t: apply(a, params.get(t)) for t, a in soft.items()})}
    meta = {"decision_id": rec["id"], "created_at": str(rec.get("created_at", "")), "domain": rec.get("domain", ""),
            "source": (rec.get("policy") or {}).get("source", ""), "policy": rec.get("policy"), "closed_world": closed}
    if fmt == "arena":
        silent = any(o["outcome"] == "silent" for o in outs)
        return [{"id": rec["id"], "domain": rec.get("domain") or ("silent" if silent else "prime"),
                 "kind": "silent" if silent else ("positive" if any(v == 1 for v in labels.values()) else "unlabeled"),
                 "state": state, "questions": qs, "candidates": [c["id"] for c in cands],
                 "positive": [k for k, v in labels.items() if v == 1],
                 "expected": [o["enzyme_id"] for o in outs if o["outcome"] == "gold"],
                 "logged": soft, "floor_ok": [c["id"] for c in cands if not c.get("rescued")]}]
    if fmt in ("laya", "kd"):
        rows = laya_rows(rec, qs, state, labels, best, closed, soft, prefer, meta, max_options, mix, outs)
        return rows if fmt == "laya" else [r for row in rows for r in kd_rows(row)]
    rules = _cand_rules(rec)
    if fmt == "sft":
        if labels:
            inject, src = [k for k, v in labels.items() if v == 1], "labels"
        else:
            name = next((t for t in prefer if t in soft), next(iter(soft), ""))
            if not name:
                return []
            rescued = {c["key"] for c in cands if c.get("rescued")}
            inject, src = teacher_select(soft[name], len(cands), rescued), f"teacher:{name}"
        user = f"# 情境\n{state}\n\n# 候选记忆\n" + "\n".join(f"[{k}] {rules.get(k, '')}" for k in rules)
        return [{"messages": [{"role": "system", "content": SFT_SYSTEM}, {"role": "user", "content": user},
                              {"role": "assistant", "content": json.dumps({"inject": inject}, ensure_ascii=False)}],
                 "meta": {**meta, "target": src}}]
    pos = [k for k, v in labels.items() if v == 1]
    neg = [k for k, v in labels.items() if v == 0]
    p_of = {c["key"]: (c.get("p") or 0.0) for c in cands}
    hard_neg = sorted(neg, key=lambda k: -p_of.get(k, 0.0))
    if fmt == "pairs":
        if not pos or not hard_neg:
            return []
        return [{"prompt": f"情境:{state}\n\n哪条记忆对处理这个情境直接有用?", "chosen": rules.get(k, ""),
                 "rejected": rules.get(hard_neg[i % len(hard_neg)], ""), "meta": meta}
                for i, k in enumerate(pos)]
    if fmt == "retrieval":
        if not pos:
            return []
        return [{"query": state, "positive": [rules.get(k, "") for k in pos],
                 "negative": [rules.get(k, "") for k in hard_neg[:5]], "meta": meta}]
    raise ValueError(f"unknown format: {fmt}")


def _ts(row: dict) -> dict:
    if isinstance(row.get("created_at"), str):
        row["created_at"] = datetime.fromisoformat(row["created_at"])
    return row


async def _from_db(since: datetime | None, page: int) -> tuple[list[dict], list[dict], dict]:
    from app.config import Config
    from app.storage.pg_store import PGStore
    from app import runtime
    pg = PGStore(Config.from_env().pg)
    await pg.connect()
    try:
        await runtime.load_admin_state(pg)      # 选择阈值等以后台配置为准
        decisions, after = [], ""
        while True:
            batch = await pg.fetch_decisions(since=since, limit=page, after_id=after)
            decisions += batch
            if len(batch) < page:
                break
            after = batch[-1]["id"]
        outcomes = await pg.fetch_outcomes(since=since)
        teachers = await pg.fetch_teacher_labels([d["id"] for d in decisions])
    finally:
        await pg.close()
    return decisions, outcomes, teachers


async def export(fmt: str, out: Path, since: datetime | None = None, sources: set[str] | None = None,
                 prefer: list[str] | None = None, page: int = 1000, max_options: int = 8,
                 mix: float = 0.0, archive: str = "", use_db: bool = True) -> dict:
    decisions, outcomes, teachers = await _from_db(since, page) if use_db else ([], [], {})
    if archive:
        # 归档(app/distill/archive.py)与库里未归档的部分合并,按主键去重;训练机上可 --no-db 只读归档
        from app.distill.archive import read_archive
        arch = read_archive(Path(archive))
        have = {d["id"] for d in decisions}
        decisions += [_ts(r) for r in arch["decision_log"] if r["id"] not in have]
        have_o = {o.get("id") for o in outcomes}
        outcomes += [_ts(r) for r in arch["decision_outcomes"] if r.get("id") not in have_o]
        for r in arch["teacher_labels"]:
            teachers.setdefault(r["decision_id"], {}).setdefault(r["teacher"], r["answers"])
        if since is not None:
            decisions = [d for d in decisions if d["created_at"] >= since]
            outcomes = [o for o in outcomes if o["created_at"] >= since]
        decisions.sort(key=lambda d: d["created_at"])
        outcomes.sort(key=lambda o: o["created_at"])
    if sources:
        decisions = [d for d in decisions if (d.get("policy") or {}).get("source", "prime") in sources]
    linked = resolve_outcomes(decisions, outcomes)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as f:
        for d in decisions:
            for row in to_formats(d, linked.get(d["id"], []), teachers.get(d["id"], {}), fmt, prefer or [],
                                  max_options=max_options, mix=mix):
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                n += 1
    return {"decisions": len(decisions), "with_outcomes": len(linked), "rows": n, "out": str(out)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--format", required=True, choices=("arena", "laya", "kd", "sft", "pairs", "retrieval"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default="", help="ISO 日期,只导出之后的判定")
    ap.add_argument("--source", default="", help="逗号分隔:prime,eval,stream,replay(默认全部)")
    ap.add_argument("--prefer", default="glm-5.3",
                    help="软标签 / sft 缺真实标签时优先采用的老师(逗号分隔);ensemble = 全体老师校准后合成")
    ap.add_argument("--max-options", type=int, default=8, help="laya:单选题最多保留几个选项(0 = 不截)")
    ap.add_argument("--mix", type=float, default=0.0, help="laya:有真实标签时混入老师概率的比例")
    ap.add_argument("--archive", default="", help="同时读取归档目录(app/distill/archive.py 的输出)")
    ap.add_argument("--no-db", action="store_true", help="只读归档,不连数据库(训练机上用)")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        # --archive 与 --since 组合时,归档/库里的时间戳都带时区:naive 按本地时区解释,
        # 否则比较直接 TypeError
        since = since.astimezone()
    sources = {s.strip() for s in args.source.split(",") if s.strip()} or None
    stats = asyncio.run(export(args.format, Path(args.out), since, sources,
                               [t.strip() for t in args.prefer.split(",") if t.strip()],
                               max_options=args.max_options, mix=args.mix,
                               archive=args.archive, use_db=not args.no_db))
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
