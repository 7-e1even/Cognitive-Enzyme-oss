"""评测指标与逐段归因 — 只做纯计算,不碰网络/DB。"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

STAGES = ("not_recalled", "killed_conditions", "rerank_failed", "rerank_dropped",
          "jev_cut", "dosage_cut", "hook_conf_filtered", "hit")


@dataclass
class QueryOutcome:
    query_id: str
    kind: str                      # "positive" | "silent"
    domain: str
    expected: list[str]
    acceptable: list[str]
    injected: list[str]            # 最终注入的酶 id(已过 hook 置信度过滤),按排序
    attribution: dict[str, str]    # expected_id -> STAGES 之一
    error: str = ""
    # 离线分析用的逐查询明细(变k模拟/floor 重扫的数据源,此前 trace 用完即弃):
    scored: list = field(default_factory=list)      # [(id, score, tier)] Stage4 排序后全量
    dosed: list = field(default_factory=list)       # 剂量截断后的 id(hook 过滤前)
    rerank_raw: list = field(default_factory=list)  # [(id, score)] floor 过滤前的 rerank 分
    # Stage 4.5(Jev)明细:[(id, p, q)] + none 概率 + 状态(ok/failed/timeout/缺省=未启用)
    jev: list = field(default_factory=list)
    jev_none: float | None = None
    jev_status: str = ""
    latency_ms: float | None = None                 # engine.match 端到端耗时
    jev_wait_ms: float | None = None                # 排序完成后等 Jev 的时间(Jev 增加的真实延迟)
    llm_judge_status: str = ""                      # 第二判官:ok/failed/timeout/缺省=未启用
    expansion: str | None = None                    # 查询扩写文本(None=未启用,""=扩写失败只用原查询)
    rerank_failed: bool = False                     # 精排重试后仍失败(限流/5xx):结果已降级,续跑时重做
    # None means the old log did not record this stage; [] is a recorded empty pool.
    recalled_ids: list[str] | None = None
    reranked_ids: list[str] | None = None
    condition_killed_ids: list[str] | None = None


def attribute_miss(expected_id: str, trace: dict, injected: list[str]) -> str:
    if expected_id in injected:
        return "hit"
    if expected_id not in trace.get("recalled_ids", []):
        return "not_recalled"
    if expected_id in trace.get("condition_killed_ids", []):
        return "killed_conditions"
    # rerank 尝试了却失败(429/5xx)→ 回退 pattern 打分、floor 门失效,
    # 下游的 dosage/hook 归因全不可信,单列成因,别让它污染其他桶。
    if trace.get("rerank_failed"):
        return "rerank_failed"
    # Jev's rescue branch can consider memories that did not survive rerank.
    # Attribute the final rejection to the stage that actually considered it.
    if expected_id in trace.get("dosed_ids", []):
        return "hook_conf_filtered"
    if trace.get("jev_status") == "ok" and any(x[0] == expected_id for x in trace.get("jev", [])):
        return "jev_cut"
    if expected_id not in trace.get("reranked_ids", []):
        return "rerank_dropped"
    if expected_id not in trace.get("dosed_ids", []):
        # Jev 生效时由它决定入选,截断归因单列,别和固定剂量的 dosage_cut 混桶
        return "jev_cut" if trace.get("jev_status") == "ok" else "dosage_cut"
    return "hook_conf_filtered"


def _agg(outcomes: list[QueryOutcome]) -> dict:
    pos = [o for o in outcomes if o.kind == "positive"]
    sil = [o for o in outcomes if o.kind == "silent"]

    exp_total = sum(len(o.expected) for o in pos)
    exp_hit = sum(len(set(o.expected) & set(o.injected)) for o in pos)

    inj_total = sum(len(o.injected) for o in pos)
    inj_rel = sum(len(set(o.injected) & (set(o.expected) | set(o.acceptable)))
                  for o in pos)

    rr = []
    for o in pos:
        rank = next((i + 1 for i, x in enumerate(o.injected) if x in o.expected), None)
        rr.append(1.0 / rank if rank else 0.0)

    hist: Counter = Counter()
    for o in pos:
        for stage in o.attribution.values():
            if stage != "hit":
                hist[stage] += 1

    return {
        "recall_at_k": exp_hit / exp_total if exp_total else 0.0,
        "precision": inj_rel / inj_total if inj_total else 1.0,
        "silence_rate": (sum(1 for o in sil if not o.injected) / len(sil)) if sil else 1.0,
        "mrr": sum(rr) / len(rr) if rr else 0.0,
        "attribution_histogram": dict(hist),
    }


def evaluate(outcomes: list[QueryOutcome]) -> dict:
    errored = [o for o in outcomes if o.error]
    ok = [o for o in outcomes if not o.error]

    report = _agg(ok)
    report["counts"] = {
        "positive": sum(1 for o in ok if o.kind == "positive"),
        "silent": sum(1 for o in ok if o.kind == "silent"),
        "error": len(errored),
    }

    by_domain: dict[str, list[QueryOutcome]] = defaultdict(list)
    for o in ok:
        if o.kind == "positive" and o.domain:
            by_domain[o.domain].append(o)
    report["by_domain"] = {d: _agg(v) for d, v in sorted(by_domain.items())}
    report["errors"] = [{"query_id": o.query_id, "error": o.error} for o in errored]
    lat = sorted(o.latency_ms for o in ok if o.latency_ms is not None)
    if lat:
        report["latency_ms"] = {"p50": lat[len(lat) // 2], "p90": lat[int(0.9 * (len(lat) - 1))],
                                "max": lat[-1]}
    jw = sorted(o.jev_wait_ms for o in ok if o.jev_wait_ms is not None)
    if jw:
        report["jev_wait_ms"] = {"p50": jw[len(jw) // 2], "p90": jw[int(0.9 * (len(jw) - 1))], "max": jw[-1]}
    llm_status = Counter(o.llm_judge_status for o in ok if o.llm_judge_status)
    if llm_status:
        report["llm_judge_status"] = dict(llm_status)
    jev_status = Counter(o.jev_status for o in ok if o.jev_status)
    if jev_status:
        report["jev_status"] = dict(jev_status)
    expansion = Counter("ok" if o.expansion else "failed" for o in ok if o.expansion is not None)
    if expansion:
        report["expansion_status"] = dict(expansion)
    degraded = sum(1 for o in ok if o.rerank_failed)
    if degraded:
        report["rerank_failed"] = degraded
    return report
