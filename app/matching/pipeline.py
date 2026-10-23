"""Matching pipeline — five-stage enzyme matching with rerank.

Stage 1: Feature extraction (done externally)
Stage 2: Qdrant vector recall (~50 candidates), fallback to PG keyword search
Stage 3: Pattern refinement (keyword + regex + conditions)
Stage 3.5: Rerank (SiliconFlow semantic reranking)
Stage 4: Relation boost + ranking
Stage 4.5: Applicability (TypeSafe Jev) — 可选;启用且成功时替代剂量截断
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

from app import settings
from app.config import EmbeddingConfig, LLMConfig, TypeSafeConfig
from app.core import Enzyme, Origin, Severity
from app.core.features import PROJECT_TAG_PREFIX
from app.matching.expand import expand_on, expand_query
from app.matching.keyword import keyword_match
from app.matching.lexical import lexical_limit, lexical_on, merge_lexical
from app.matching.llm_judge import judge_llm, judge_llm_enabled, judge_llm_fallback_enabled
from app.matching.regex_matcher import regex_match
from app.matching.rerank import rerank
from app.matching.route import await_route, pick_domains, route_mode
from app.matching.scorer import compute_score, check_conditions, conditions_mode
from app.matching.selection import analogy_mode, thresholds
from app.storage.pg_store import PGStore
from app.storage.qdrant_store import QdrantStore
from app.typesafe_client import Applicability, fit_pool, judge_applicability, judge_two_stage

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    enzyme: Enzyme
    score: float
    tier: str  # "critical" | "relevant" | "peripheral"
    exploratory: bool = False  # 探索槽注入的未验证假设(dream/mutant/crossover)
    applicability: float | None = None  # Stage 4.5 Jev noul:对本次请求直接有用的概率
    reason: str = ""  # 入选路径:primary/supplement/critical/analogy/vote/hint/explorer/dosage/fallback/llm_fallback(判定记录用)


# ── 探索槽(exploration slot)────────────────────────────────────
# dream/mutation/crossover 产出的假设酶出生 confidence 低(0.2~0.5),
# 既过不了 hook 端置信度过滤,也挤不进剂量预算 —— 没有上场机会就永远
# 无法被验证,演化回路开环。探索槽以小概率在剂量之外追加一条与当前
# 上下文最相关的假设酶(明确标注,由 hook 端豁免置信度过滤),让它进入
# report/被动评估的验证循环:被证实则 confidence 上涨、自然入选;
# 被证伪则 hebbian 降权、immune 修剪。
# 评测脚本把它设为 0.0 以保证确定性;None = 读运行参数 ranking.exploration_rate
_EXPLORATION_RATE: float | None = None
_EXPLORATION_MAX_CONF = 0.6  # 高于此的已接近自证,不占探索名额
# dream→EVOLVED / mutation→MUTATED / crossover→CROSSED
_EXPLORATION_ORIGINS = {Origin.EVOLVED, Origin.MUTATED, Origin.CROSSED}

# ── 项目维度(project scope)─────────────────────────────────────
# 酶库跨项目共用时,A 项目的本地知识会污染 B 项目的召回。带 project: 标签的
# 酶按出身加权:同项目小幅加成,异项目明显降权;无标签(通用酶)不受影响。
# ponytail: 固定系数,要按 feedback_coverage 调优再提成配置
_PROJECT_BOOST = 1.15
_PROJECT_PENALTY = 0.7


def _project_factor(tags: list[str], project: str) -> float:
    """按酶的项目出身返回打分系数。无项目上下文或酶无项目标签 → 1.0。"""
    if not project:
        return 1.0
    ptags = {t[len(PROJECT_TAG_PREFIX):] for t in tags
             if t.startswith(PROJECT_TAG_PREFIX)}
    if not ptags:
        return 1.0
    return _PROJECT_BOOST if project in ptags else _PROJECT_PENALTY


def _exploration_rate() -> float:
    return _EXPLORATION_RATE if _EXPLORATION_RATE is not None else settings.get("ranking.exploration_rate")


def _relevance_floor() -> float:
    """ranking.relevance_floor(CE_RELEVANCE_FLOOR),默认 0.01。"""
    return settings.get("ranking.relevance_floor")


def _condition_boost() -> float:
    """soft 模式下,conditions 命中时给 pattern_score 的加成(ranking.conditions_boost)。"""
    return settings.get("ranking.conditions_boost")


def _condition_boost_stage() -> str:
    """ranking.conditions_boost_stage(CE_CONDITIONS_BOOST_STAGE):conditions 加成生效阶段。

    - "pattern"(默认,现状行为):加成只加在 Stage 3 的 pattern_score 上——
      round-2 sweep 证实这在 rerank 路径上是死代码,rerank 分支会用 rr.score
      整体替换掉 pattern_score,加成被连带吃掉。
    - "rerank":除 Stage 3 照常加成外,rerank 替换分数之后针对命中 conditions
      的候选再补一次加成,让它在语义精排后仍然保留区分度。
    非法值回退 "pattern"。
    """
    return settings.get("ranking.conditions_boost_stage")


def _condition_hit(enzyme: Enzyme, context_lower: str) -> bool:
    """soft 模式下 enzyme 的 conditions 是否命中 context——Stage 3 与
    rerank 后补加成共享的判断,保持匹配语义一致。"""
    return bool(enzyme.conditions) and any(
        cond.lower() in context_lower for cond in enzyme.conditions
    )


def _dosage_budget() -> int:
    """ranking.dosage_budget(CE_DOSAGE_BUDGET):剂量预算基数(默认 5)。"""
    return settings.get("ranking.dosage_budget")


def _rerank_topn_mult() -> float:
    """rerank top_n = max_results * 此系数(ranking.rerank_topn_mult,默认 2)。

    诊断(knobF,见 reports/knobF.md):rerank_dropped 抽样 1/7(q026)是排名第14的
    golden 被 top_n=12(=max_results*2)截断,分数本身在 floor 之上——floor 调整救不了
    这类候选,只有放宽 top_n 才有用。
    """
    return settings.get("ranking.rerank_topn_mult")


# ── Stage 4.5 适用性判定(TypeSafe Jev)────────────────────────────
# 固定剂量(≤5 条)对"相关池中位数 2~3 条"的请求天然注水,precision 的结构天花板
# 就在这里(tests/eval/README.md)。Jev 给每条候选一个校准过的"对本请求直接有用"
# 概率 p,再用一道 choice 做相对比较(q / none),据此决定推哪几条、推几条:
#   主选  = choice 最高项(q ≥ CHOICE_MIN 且 p ≥ PRIMARY_P)
#   补充  = p ≥ SUPPLEMENT_P 且 q 不太弱(≥ 最高 q 的 10%)——同一请求确实需要两条时
#   告警  = severity=critical 且 p ≥ CRITICAL_P——"不压制真警报"的弱化版
#   静默  = choice 判 none ≥ NONE_MAX 时不补充任何条目
# 位于 relevance floor 下游(floor 本身不动,静默纪律靠它兜底)。离线标定见
# tests/eval/jev_gate/README.md。未配置 key / 超时 / 出错 → 原剂量逻辑,逐位等价。
def _jev_stage() -> str:
    """judge.stage(CE_JEV_STAGE): post_rerank(默认,准确度优先)| parallel(省延迟)。

    post_rerank:判定池 = rerank 排好序的 floor 幸存者 + 召回序余量(救援用),精排高位
    一定被判到;串行多约 0.5s。parallel:与 rerank 并行判定召回序前 N 条,延迟被 rerank
    掩盖,但精排高位若在召回序 N 名外就判不到。本地真实链路 A/B(池 24 + 救援,
    tests/eval/jev_gate/README.md):post_rerank R 86% / P 95.0% / MRR 0.820、Jev 截断 2;
    parallel R 85% / P 94.9% / MRR 0.815、Jev 截断 4;静默率都是 100%。"""
    return settings.get("judge.stage")


def _jev_pool() -> int:
    """judge.pool(CE_JEV_POOL):每次 prime 判定的候选上限(默认 36)。本地真实链路 A/B:24 → R 86% /
    MRR 0.820,36 → R 88% / MRR 0.840(精排高位之外的候选也能被判到),50 → R 85% / MRR 0.820
    (choice 选项过多,概率被摊薄、干扰项增多)。"""
    return settings.get("judge.pool")


def _rrf_pool(recall_order: list[Enzyme], rerank_order: list[Enzyme], n: int, k: int = 60) -> list[Enzyme]:
    """两路名次融合(RRF,k=60)取前 n 条;同分按召回序。"""
    score: dict[str, float] = {}
    by_id: dict[str, Enzyme] = {}
    for order in (recall_order, rerank_order):
        for rank, e in enumerate(order):
            score[e.id] = score.get(e.id, 0.0) + 1.0 / (k + rank + 1)
            by_id.setdefault(e.id, e)
    first = {e.id: i for i, e in enumerate(recall_order)}
    ids = sorted(score, key=lambda i: (-score[i], first.get(i, len(first))))
    return [by_id[i] for i in ids[:n]]


def _jev_rescue() -> bool:
    """judge.rescue(CE_JEV_RESCUE),默认开。见 Stage 4.5 救援说明。"""
    return settings.get("judge.rescue")


def _jev_enabled(typesafe_config: TypeSafeConfig | None) -> bool:
    return bool(typesafe_config and typesafe_config.available and settings.get("judge.enabled"))


async def _run_pipeline_jev(
    coro_or_task, trace: dict | None, started_at: float | None = None,
) -> Applicability | None:
    """等待 Jev 判定;任何异常/超时都归为 None(热路径绝不因 Jev 失败)。

    超时从 Jev 请求发出时(started_at)起算,不是从这里开始等时起算:parallel 模式下
    Jev 与 rerank 重叠,热路径上 Jev 的最坏占用是 max(rerank, judge.timeout)。
    trace["jev_wait_ms"] 记"排序完成后还要等 Jev 多久"——这才是 Jev 增加的真实延迟。"""
    timeout = settings.get("judge.timeout")
    t0 = time.perf_counter()
    remaining = max(0.01, timeout - (t0 - started_at)) if started_at is not None else timeout
    try:
        return await asyncio.wait_for(coro_or_task, timeout=remaining)
    except asyncio.TimeoutError:
        logger.warning("Jev applicability timed out (>%.1fs), falling back to dosage", timeout)
        if trace is not None:
            trace["jev_status"] = "timeout"
    except Exception:
        logger.exception("Jev applicability failed (non-fatal)")
    finally:
        if trace is not None:
            trace["jev_wait_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return None


def _analogy_candidates(enzymes: list[Enzyme]) -> list[Enzyme]:
    """过往适用情境与当前情境最像的前 k 条记忆(每条带它最像的那条经验样本)。"""
    if analogy_mode() == "off":
        return []
    k = settings.get("exemplar.analogy_k")
    hinted = {e.id: e for e in enzymes if e._hint}
    return sorted(hinted.values(), key=lambda e: -e._hint_score)[:k]


def _start_judges(
    context: str,
    pool: list[Enzyme],
    typesafe_config: TypeSafeConfig,
    llm_config: LLMConfig | None,
    analogies: list[Enzyme] | None = None,
) -> tuple["asyncio.Task", "asyncio.Task | None", float, list[Enzyme]]:
    """同时起跑 Jev(主判官)与可选的 LLM 第二判官(融合)。返回 (jev 任务, llm 任务 | None,
    起跑时刻, fit_pool 截后的池)。judge.two_stage 开时 Jev 任务拿未截的原池——两段判定自己
    分批扫 scan_pool;融合/接手的大模型仍判返回的这个截后池。"""
    started = time.perf_counter()
    raw_pool = pool
    pool = fit_pool(context, pool)
    if settings.get("judge.two_stage"):
        jev = asyncio.create_task(judge_two_stage(
            context, raw_pool, typesafe_config,
            timeout=settings.get("judge.timeout"), analogies=analogies))
    else:
        jev = asyncio.create_task(judge_applicability(
            context, pool, typesafe_config, timeout=settings.get("judge.timeout"), analogies=analogies))
    llm = None
    if judge_llm_enabled(llm_config):
        llm = asyncio.create_task(judge_llm(
            context, pool, llm_config, timeout=settings.get("judge.llm_timeout")))
    return jev, llm, started, pool


async def _collect_judges(
    jev_task, llm_task, started: float, trace: dict | None,
    llm_config: LLMConfig | None, context: str, pool: list[Enzyme],
) -> tuple[Applicability | None, dict[str, float] | None]:
    """等判官结果,返回 (Jev 判定, LLM 接手概率)。

    Jev 成功:开了第二判官融合(judge.llm)就按 judge.llm_timeout 等它并与 Jev 的 noul 取平均。
    Jev 失败/超时:开了接手判定(judge.llm_fallback,默认开)才在此刻调大模型——顺序兜底,
    Jev 正常时一次都不多调。融合任务已在跑就复用它(等到它自己的 judge.llm_timeout 截止,
    不发起第二次调用);否则新起 judge_llm,只等 judge.llm_fallback_timeout。
    LLM 及时作答返回 (None, llm_p),由调用方按 llm_fallback 规则注入。"""
    app = await _run_pipeline_jev(jev_task, trace, started_at=started)
    if app is None:
        if not judge_llm_fallback_enabled(llm_config):
            if llm_task is not None:
                llm_task.cancel()
            return None, None
        try:
            if llm_task is not None:   # 融合开着的已经在跑,复用它
                remaining = max(0.01, settings.get("judge.llm_timeout") - (time.perf_counter() - started))
                llm_p = await asyncio.wait_for(llm_task, timeout=remaining)
            else:
                fb_timeout = settings.get("judge.llm_fallback_timeout")
                llm_p = await asyncio.wait_for(
                    judge_llm(context, pool, llm_config, timeout=fb_timeout), timeout=fb_timeout)
        except asyncio.TimeoutError:
            llm_p = None
            if trace is not None:
                trace["llm_judge_status"] = "timeout"
        except Exception:
            logger.exception("LLM fallback judge failed (non-fatal)")
            llm_p = None
        if llm_p is None:
            if trace is not None:
                trace.setdefault("llm_judge_status", "failed")
            return None, None
        if trace is not None:
            trace["llm_judge_status"] = "ok"
            trace["judge"] = "llm_fallback"
        return None, llm_p
    if llm_task is None:
        return app, None
    remaining = max(0.01, settings.get("judge.llm_timeout") - (time.perf_counter() - started))
    try:
        llm_p = await asyncio.wait_for(llm_task, timeout=remaining)
    except asyncio.TimeoutError:
        llm_p = None
        if trace is not None:
            trace["llm_judge_status"] = "timeout"
    except Exception:
        logger.exception("LLM judge failed (non-fatal)")
        llm_p = None
    if llm_p is None:
        if trace is not None:
            trace.setdefault("llm_judge_status", "failed")
        return app, None
    if trace is not None:
        trace["llm_judge_status"] = "ok"
        trace["llm_p"] = [(eid, llm_p[eid]) for eid in app.p if eid in llm_p]
    fused = {eid: (pj + llm_p[eid]) / 2 if eid in llm_p else pj for eid, pj in app.p.items()}
    return dataclasses.replace(app, p=fused), None


def _apply_jev_dosage(
    results: list[MatchResult],
    app: Applicability,
    max_results: int,
    rng: Callable[[], float] = random.random,
    rescued_ids: set[str] | None = None,
) -> list[MatchResult]:
    """按 Jev 判定选择注入条目(替代固定剂量)。未被判定的候选不入选。

    ``rescued_ids``:被 rerank/floor 淘汰、经救援回到候选池的条目。两个判官意见相反时
    要更强的证据才推翻精排——任何入选路径都要求 p ≥ selection.rescue_p(默认 0.9)或单选
    ≥ selection.rescue_q(默认 0.7)。离线复核(jevL_par24R 轨迹):该门把静默率 90%→100%,Recall 不变。

    阈值统一出自运行参数 selection.*(app/settings.py;离线评估老师、导出、判定记录与这里共用)。"""
    rescued_ids = rescued_ids or set()
    t = thresholds()
    rescue_p = t["rescue_p"]
    # 单选题的相对判断很确定时也给它话语权:知识类记忆(缺陷、判例、论文)按"照做会不会更好"
    # 天然拿不到高 noul,但 choice 选得很准。真实基准离线复核(selsim,当时取 0.8/0.9):
    # Recall 38.4%→44.6%,precision 82.2%→82.0%,静默 100% 不变;现行 0.6/0.7 见 selection.py。
    rescue_q = t["rescue_q"]
    choice_override = t["choice_override"]
    judged = [r for r in results if r.enzyme.id in app.p]
    for r in judged:
        r.applicability = app.p[r.enzyme.id]
    if not judged:
        return []

    supplement_q = getattr(app, "supplement_q", {})
    primary_judged = [r for r in judged if r.enzyme.id in app.q]

    def q(r: MatchResult) -> float:
        return app.q.get(r.enzyme.id, supplement_q.get(r.enzyme.id, 0.0))

    def p(r: MatchResult) -> float:
        return app.p[r.enzyme.id]

    def eligible(r: MatchResult) -> bool:
        return r.enzyme.id not in rescued_ids or p(r) >= rescue_p or q(r) >= rescue_q

    q_max = max((q(r) for r in (primary_judged or judged)), default=0.0)
    none_ok = app.none < t["none_max"]
    supplement_p = t["supplement_p"]
    selected: list[MatchResult] = []
    top = max(primary_judged or judged, key=lambda r: (q(r), p(r)))
    if ((q(top) >= t["choice_min"] and p(top) >= t["primary_p"])
            # 近重复记忆会把 choice 概率摊薄(各 0.2),谁都过不了主选线;此时
            # noul 极高且 choice 没判 none,仍取最高项,避免"都适用反而都不推"
            or (none_ok and p(top) >= supplement_p)
            or q(top) >= choice_override) and eligible(top):
        top.reason = "primary"
        selected.append(top)

    if none_ok:
        critical_p = t["critical_p"]
        for r in sorted(judged, key=p, reverse=True):
            if r in selected:
                continue
            if not eligible(r):
                continue
            if r.enzyme.id in supplement_q and r.enzyme.id not in app.q:
                if (p(r) >= rescue_p or q(r) >= rescue_q
                        or (selected and p(r) >= supplement_p and q(r) >= 0.1 * q_max)):
                    r.reason = "supplement"
                    selected.append(r)
                continue
            if selected and p(r) >= supplement_p and q(r) >= 0.1 * q_max:
                r.reason = "supplement"
                selected.append(r)
            elif r.enzyme.severity == Severity.CRITICAL and p(r) >= critical_p:
                r.reason = "critical"
                selected.append(r)

    # 类比识别(CE_EXEMPLAR_ANALOGY,默认 on):Jev 认定当前情境与某条记忆过去适用过的情境是同一类情况
    # (单选题 ≥ CE_ANALOGY_Q 且高于 none,逐条 noul ≥ CE_ANALOGY_P)→ 入选。这是独立于"抽象记忆
    # 有没有用"的另一路证据,不受适用性 none 门约束。离线(真实经验流,过去情境按 0.5 概率学会,
    # BM25 提名 3 条):法律 精度 89% / 认回 40% 熟悉情境,Firefox 83% / 37%;只看单选题时
    # Firefox 新问题多、误认多(精度 38%),两道题都要过。
    if analogy_mode() == "on" and app.analog_q:
        eid = max(app.analog_q, key=lambda k: (app.analog_q[k], app.analog_p.get(k, 0.0)))
        aq, ap = app.analog_q[eid], app.analog_p.get(eid, 0.0)
        if aq >= t["analogy_q"] and ap >= t["analogy_p"] and aq > app.analog_none:
            hit = next((r for r in judged if r.enzyme.id == eid), None)
            if hit is not None and hit not in selected:
                hit.reason = "analogy"
                selected.append(hit)

    # 方法论提示(selection.hint_max,默认 2):通用方法论(scope:general)是提示而不是事实——推错了
    # agent 可以自己判断后忽略,代价远低于推错一条具体知识。hardbench 物理经验流诊断(2026-09-26):
    # 解题时 Jev 对"用极限情形自检"这类方法论给 p≈0.4–0.5,却常判 none≈0.7,按知识类记忆标定的
    # none 门把它们全部拦下。这条通道不受 none 门约束,按 p 取至多 hint_max 条。
    hint_max = int(t.get("hint_max") or 0)
    if hint_max:
        hints = [r for r in sorted(judged, key=p, reverse=True)
                 if r not in selected and "scope:general" in r.enzyme.tags and p(r) >= t["hint_p"] and eligible(r)]
        for r in hints[:hint_max]:
            r.reason = "hint"
            selected.append(r)

    selected = selected[:max_results]

    # 冷启动解锁:hook 端按酶自身 confidence 过滤(默认 0.25),新酶/假设酶即使此刻
    # 高度适用也会被滤掉 → 永远拿不到反馈 → 永远升不上来。Jev 高度确认适用的,
    # 标为探索项(hook 豁免置信度过滤并标注"未验证"),让它进入 report/被动评估循环。
    # 只解锁"未证伪"的:被反馈反复否定而跌下来的酶,Jev 只看语义照样会判适用,
    # 放出来等于把学习成果推翻——失败多于成功、或试用次数已够的不再解锁。
    promote_below = settings.get("judge.promote_below")
    promote_p = settings.get("judge.promote_p")
    max_trials = settings.get("judge.promote_max_trials")
    for r in selected:
        e = r.enzyme
        if (e.confidence < promote_below and p(r) >= promote_p
                and e.false_positive_count <= e.success_count and e.trigger_count < max_trials):
            r.exploratory = True
            break  # 每次 prime 至多一条,避免假设挤占注入

    explorer = _pick_explorer(results, selected, max_results, rng,
                              min_p=settings.get("judge.explore_p"), app=app)
    if explorer:
        explorer.exploratory = True
        explorer.reason = "explorer"
        selected.append(explorer)
    return selected


async def run_pipeline(
    context: str,
    search_query: str,
    pg: PGStore,
    qdrant: QdrantStore,
    domain: str | None = None,
    max_results: int = 15,
    embedding_config: EmbeddingConfig | None = None,
    project: str = "",
    trace: dict | None = None,
    typesafe_config: TypeSafeConfig | None = None,
    llm_config: LLMConfig | None = None,
    expand_config: LLMConfig | None = None,
    judge_llm_config: LLMConfig | None = None,
) -> list[MatchResult]:
    """Execute the five-stage matching pipeline.

    Uses Qdrant vector search for recall with PG keyword fallback.
    Falls back to pattern scoring if rerank is unavailable.
    ``trace``（可选 dict）会被填入各阶段候选 id，供评测归因，生产路径传 None 零开销。
    ``expand_config`` / ``judge_llm_config``:查询扩写与 LLM 第二判官用的模型,缺省用 ``llm_config``。
    """
    judge_llm_config = judge_llm_config or llm_config
    # ── Stage 2: Recall ──
    recalled = await _recall(search_query, context, pg, qdrant, domain, embedding_config,
                             llm_config=expand_config or llm_config, trace=trace, typesafe_config=typesafe_config)

    if trace is not None:
        trace["recalled_ids"] = [e.id for e in recalled]
        trace["hinted_ids"] = [e.id for e in recalled if e._hint]      # 被经验样本认出的候选
        trace["hint_scores"] = {e.id: round(e._hint_score, 4) for e in recalled if e._hint}

    if not recalled:
        return []

    logger.debug("Stage 2 recall: %d candidates", len(recalled))

    # ── Stage 3: Pattern refinement ──
    refined: list[tuple[Enzyme, float]] = []
    condition_killed: list[str] = []
    soft_conditions = conditions_mode() == "soft"
    context_lower = context.lower() if soft_conditions else ""
    for enzyme in recalled:
        if not check_conditions(enzyme, context):
            condition_killed.append(enzyme.id)
            continue

        kw_score = keyword_match(enzyme, context)
        rx_score = regex_match(enzyme, context)
        pattern_score = max(kw_score, rx_score)

        # If neither keyword nor regex matched, use recall relevance as baseline
        if pattern_score == 0:
            pattern_score = 0.3

        # soft 模式:conditions 不淘汰候选,但命中时仍给排序区分度一点加成
        # (hard 模式下这条信息已经体现在"能通过 check_conditions"本身)。
        if soft_conditions and _condition_hit(enzyme, context_lower):
            pattern_score = min(1.0, pattern_score + _condition_boost())

        refined.append((enzyme, pattern_score))

    logger.debug("Stage 3 refinement: %d matched", len(refined))

    if trace is not None:
        trace["condition_killed_ids"] = condition_killed
        trace["refined_ids"] = [e.id for e, _ in refined]

    # ── Stage 4.5(parallel 模式)提前起跑:与 rerank 并行判定召回序前 N 条 ──
    jev_on = _jev_enabled(typesafe_config) and bool(refined)
    judges = None
    if jev_on and _jev_stage() == "parallel":
        pool = [e for e, _ in refined[:_jev_pool()]]
        analogies = _analogy_candidates([e for e, _ in refined])
        if analogy_mode() == "on":
            have = {e.id for e in pool}
            pool += [e for e in analogies if e.id not in have]
        judges = _start_judges(context, pool, typesafe_config, judge_llm_config, analogies)
    try:
        return await _rank_and_dose(
            context, refined, pg, max_results, embedding_config, project, trace,
            soft_conditions, context_lower, typesafe_config if jev_on else None,
            judge_llm_config, judges)
    finally:
        for task in (judges or (None, None))[:2]:
            if task is not None and not task.done():
                task.cancel()


async def _rank_and_dose(
    context: str,
    refined: list[tuple[Enzyme, float]],
    pg: PGStore,
    max_results: int,
    embedding_config: EmbeddingConfig | None,
    project: str,
    trace: dict | None,
    soft_conditions: bool,
    context_lower: str,
    typesafe_config: TypeSafeConfig | None,
    llm_config: LLMConfig | None = None,
    judges: tuple | None = None,
) -> list[MatchResult]:
    """Stage 3.5 ~ 4.5:rerank + floor → 打分排序 → 适用性判定/剂量截断。"""
    recalled_pool = list(refined)  # rerank/floor 前的候选(召回序),Jev 救援用

    # ── Stage 3.5: Rerank ──
    rerank_attempted = bool(embedding_config and refined)
    rerank_ok = False
    if embedding_config and refined:
        documents = [
            f"{e.patterns_readable} | {e.then} | {e.because}"
            + (f" | 过往适用情境: {e._hint[:300]}" if e._hint and _exemplar_hint_on() else "")
            for e, _ in refined
        ]
        rerank_results = await rerank(
            query=context,
            documents=documents,
            config=embedding_config,
            # Keep more than needed for Stage 4 boost; env-able for A/B (knobF)。
            # rerank_keep=floor:全部打分,只靠下面的相关性下限淘汰——按名次截会丢掉高分候选。
            top_n=(len(documents) if settings.get("ranking.rerank_keep") == "floor"
                   else int(max_results * _rerank_topn_mult())),
        )
        if rerank_results:
            rerank_ok = True
            # Replace pattern_score with rerank relevance_score.
            # CE_CONDITIONS_BOOST_STAGE=rerank: 分数被整体替换后,针对命中
            # conditions 的候选再补一次加成(见 _condition_boost_stage 说明)。
            apply_rerank_boost = soft_conditions and _condition_boost_stage() == "rerank"
            rerank_boost = _condition_boost() if apply_rerank_boost else 0.0
            reranked: list[tuple[Enzyme, float]] = []
            for rr in rerank_results:
                if rr.index < len(refined):
                    enzyme, _ = refined[rr.index]
                    score = rr.score
                    if apply_rerank_boost and _condition_hit(enzyme, context_lower):
                        score = min(1.0, score + rerank_boost)
                    reranked.append((enzyme, score))
            refined = reranked
            logger.debug("Stage 3.5 rerank: %d results", len(refined))

            # floor 过滤前的原始 rerank 分数——评测离线分析(变k模拟/floor 重扫)
            # 的数据源;生产 trace=None 零开销。
            if trace is not None:
                trace["rerank_raw"] = [(e.id, s) for e, s in refined]

            # 相关性下限:rerank 分数低于此线的候选直接淘汰,不进入 Stage 4——
            # 否则打分公式总能给任意候选凑出正分,精排的"不相关"判断被稀释掉。
            # 评测 knobC: 0.01 是本次唯一能让静默率从 0%→100%(达标)的旋钮,
            # 且 recall/precision/mrr 同步改善,无指标恶化,已定为默认值。
            # bge-reranker-v2-m3 分数两极分化明显(不相关 ~1e-4~1e-5, 相关 >0.1),
            # 0.01 留有余量。env 覆盖仍可关闭(设为 0)。
            relevance_floor = _relevance_floor()
            if relevance_floor > 0:
                refined = [(e, s) for e, s in refined if s >= relevance_floor]

    if trace is not None:
        trace["reranked_ids"] = [e.id for e, _ in refined]
        # rerank 尝试了却失败(429/5xx/超时)→ 回退 pattern 打分,floor 门失效。
        # 不标记的话,这类 miss 会被评测误归因为 dosage_cut/hook_conf_filtered。
        trace["rerank_failed"] = rerank_attempted and not rerank_ok

    # ── Stage 4: Relation boost + ranking ──
    # Gain modulation: estimate urgency from candidate severity distribution
    urgency = 0.5
    if refined:
        critical_count = sum(1 for e, _ in refined if e.severity == Severity.CRITICAL)
        urgency = min(1.0, critical_count / len(refined) * 2)

    results: list[MatchResult] = []
    activated_ids = {e.id for e, _ in refined}

    # 关系查询批量化(单条窗口函数查询替代每候选一次往返)。
    # 降级差异:批量失败废掉本轮全部 rel_boost(原逐条版只废单条)——仅 DB
    # 故障场景,正常路径逐位等价。
    try:
        related_map = await pg.get_related_batch([e.id for e, _ in refined], limit=5)
    except Exception:
        logger.warning("Batch relation query failed; relation boosts dropped for this round", exc_info=True)
        related_map = {}

    for enzyme, pattern_score in refined:
        rel_boost = 0.0
        for rel in related_map.get(enzyme.id, []):
            if rel.get("id") in activated_ids:
                rel_boost += 0.15 * rel.get("rel_weight", 1.0)

        score = compute_score(enzyme, pattern_score, rel_boost, urgency=urgency)
        score *= _project_factor(enzyme.tags, project)

        if enzyme.severity == Severity.CRITICAL or score > 0.7:
            tier = "critical"
        elif score > 0.3:
            tier = "relevant"
        else:
            tier = "peripheral"

        results.append(MatchResult(enzyme=enzyme, score=score, tier=tier))

    tier_order = {"critical": 0, "relevant": 1, "peripheral": 2}
    results.sort(key=lambda r: (tier_order[r.tier], -r.score))

    if trace is not None:
        trace["scored"] = [(r.enzyme.id, r.score, r.tier) for r in results]

    # ── Stage 4.5: Applicability(Jev)──
    # 救援(CE_JEV_RESCUE=on):被 rerank/floor 淘汰、但 Jev 明确判适用的候选也可入选——
    # "具体情境 × 抽象原则"的间接匹配正是精排弱区(rerank_dropped 是最大 miss 桶)。
    # 静默纪律由 choice 的 none 门兜底(_apply_jev_dosage)。
    rescue = _jev_rescue()
    jev_attempted = False
    if typesafe_config is not None and (results or (rescue and recalled_pool)):
        jev_attempted = True
        if trace is not None:
            trace["jev_status"] = "failed"
        if judges is None:
            if settings.get("judge.pool_order") == "rrf" and rerank_ok:
                # 召回序与精排序按名次融合:两路各自漏掉的正例互补,置信度 / 新近度等先验不参与组池——
                # 否则从未触发过的新记忆排不进判定池,永远拿不到反馈。精排淘汰项照常走救援门。
                pool = _rrf_pool([e for e, _ in recalled_pool], [e for e, _ in refined], _jev_pool())
            else:
                pool = [r.enzyme for r in results[:_jev_pool()]]
                if rescue:
                    have = {e.id for e in pool}
                    pool += [e for e, _ in recalled_pool if e.id not in have][:max(0, _jev_pool() - len(pool))]
            analogies = _analogy_candidates([e for e, _ in recalled_pool])
            if analogy_mode() == "on":                              # 类比候选必须被判到才能入选
                have = {e.id for e in pool}
                pool += [e for e in analogies if e.id not in have]
            judges = _start_judges(context, pool, typesafe_config, llm_config, analogies)
        try:
            app, llm_p = await _collect_judges(judges[0], judges[1], judges[2], trace,
                                               llm_config, context, judges[3])
        finally:
            for task in judges[:2]:
                if task is not None and not task.done():
                    task.cancel()
        if app is None and llm_p is not None:
            # Jev 失败/超时、大模型接手:按它的概率注入(危急项照常),不救精排淘汰项——
            # 救援需要 Jev 单选题的相对证据,LLM 只给逐条概率。
            dosed = _apply_llm_fallback(results, llm_p, max_results)
            if trace is not None:
                trace["dosed_ids"] = [r.enzyme.id for r in dosed]
                trace["llm_p"] = [(eid, p) for eid, p in llm_p.items()]
            return dosed
        if app is not None:
            candidates = list(results)
            rescued: set[str] = set()
            if rescue:
                have = {r.enzyme.id for r in results}
                extra = [MatchResult(enzyme=e, score=compute_score(e, 0.0), tier="peripheral")
                         for e, _ in recalled_pool if e.id in app.p and e.id not in have]
                candidates += extra
                rescued = {r.enzyme.id for r in extra}
            dosed = _apply_jev_dosage(candidates, app, max_results, rescued_ids=rescued)
            if trace is not None:
                trace["jev_status"] = "ok"
                supplement_q = getattr(app, "supplement_q", {})
                trace["jev"] = [(eid, app.p[eid], app.q.get(eid, supplement_q.get(eid, 0.0))) for eid in app.p]
                trace["jev_none"] = app.none
                trace["jev_model"] = app.model
                trace["jev_mode"] = app.mode
                trace["jev_calls"] = app.calls
                trace["jev_request"], trace["jev_answers"], trace["jev_pool"] = app.request, app.raw, app.pool
                if getattr(app, "supplement_request", None) is not None:
                    trace["jev_supplement_request"] = app.supplement_request
                    trace["jev_supplement_answers"] = getattr(app, "supplement_raw", None)
                if app.analog_q:
                    trace["analogy"] = [(eid, app.analog_p.get(eid, 0.0), q) for eid, q in app.analog_q.items()]
                    trace["analogy_none"] = app.analog_none
                trace["dosed_ids"] = [r.enzyme.id for r in dosed]
            return dosed

    # 判官开着却没给出判定(失败/超时):不再按固定剂量注满——hardbench 实测失灵后注入
    # 5 条未判定记忆把稳定题变错率从 3.8% 抬到 8.2%。只放危急项与至多
    # judge.fallback_budget 条非危急项,不挑探索项。判官没开(未配置/关闭)照旧 _apply_dosage。
    if jev_attempted:
        dosed = _apply_fallback(results, max_results)
    else:
        dosed = _apply_dosage(results, max_results)
    if trace is not None:
        trace["dosed_ids"] = [r.enzyme.id for r in dosed]
    return dosed


def _apply_dosage(
    results: list[MatchResult],
    max_results: int,
    rng: Callable[[], float] = random.random,
) -> list[MatchResult]:
    """Adaptive dosage — inject a few high-salience signals, not a wall of warnings.

    A System-1 layer that floods every prompt with 15 alerts becomes noise the
    agent learns to ignore. So: keep ALL critical-tier hits (never suppress a
    real alarm), then admit only a shrinking budget of lower-tier suggestions —
    the more criticals are already firing, the less room for peripheral chatter.
    ``max_results`` remains the hard ceiling the caller asked for.

    最后以探索槽概率(ranking.exploration_rate)在剂量之外追加一条探索槽假设酶(见上),
    仍受 ``max_results`` 硬顶约束。``rng`` 可注入以便测试。
    """
    critical = [r for r in results if r.tier == "critical"]
    rest = [r for r in results if r.tier != "critical"]
    # 0 crit → 预算个 extras, 预算-1+ crit → 1(CE_DOSAGE_BUDGET 默认 5)
    budget = max(1, _dosage_budget() - len(critical))
    dosed = (critical + rest[:budget])[:max_results]
    for r in dosed:
        r.reason = "dosage"

    explorer = _pick_explorer(results, dosed, max_results, rng)
    if explorer:
        explorer.exploratory = True
        explorer.reason = "explorer"
        dosed.append(explorer)
    return dosed


def _apply_fallback(results: list[MatchResult], max_results: int) -> list[MatchResult]:
    """判官尝试过但失败/超时时的保守回退:危急级记忆(severity=critical)照常放行,
    其余——包括仅因分数高而落入 critical 层的——只放前 judge.fallback_budget 条
    (默认 0 = 判官不判就不注入),不挑探索项。"""
    critical = [r for r in results if r.enzyme.severity == Severity.CRITICAL]
    rest = [r for r in results if r.enzyme.severity != Severity.CRITICAL]
    dosed = (critical + rest[:settings.get("judge.fallback_budget")])[:max_results]
    for r in dosed:
        r.reason = "fallback"
    return dosed


def _apply_llm_fallback(
    results: list[MatchResult],
    llm_p: dict[str, float],
    max_results: int,
) -> list[MatchResult]:
    """Jev 没作答、大模型接手判定时的注入:危急级记忆(severity=critical)照常放行,
    其余——包括仅因分数高而落入 critical 层的——取大模型适用概率 ≥ judge.llm_fallback_p
    的前 judge.llm_fallback_max 条(按 p 降序);不挑探索项。
    阈值按 Jev 的选择标定(物理 56 题,GLM-5.3 关思考):0.8 → 精度 80% / 召回 65%。"""
    critical = [r for r in results if r.enzyme.severity == Severity.CRITICAL]
    threshold = settings.get("judge.llm_fallback_p")
    picked = sorted((r for r in results
                     if r.enzyme.severity != Severity.CRITICAL
                     and llm_p.get(r.enzyme.id, -1.0) >= threshold),
                    key=lambda r: llm_p[r.enzyme.id], reverse=True)
    dosed = (critical + picked[:settings.get("judge.llm_fallback_max")])[:max_results]
    for r in dosed:
        r.reason = "llm_fallback"
        if r.enzyme.id in llm_p:   # 危急项可能没被判到(被 token 预算截掉)
            r.applicability = llm_p[r.enzyme.id]
    return dosed


def _pick_explorer(
    results: list[MatchResult],
    dosed: list[MatchResult],
    max_results: int,
    rng: Callable[[], float],
    min_p: float = 0.0,
    app: Applicability | None = None,
) -> MatchResult | None:
    """从落选结果里挑最相关的一条假设酶。探索只让渡置信度,不让渡相关性:
    候选已通过召回+条件检查+打分,按序取第一条即"此情境下最像会有用的假设"。"""
    rate = _exploration_rate()
    if rate <= 0 or len(dosed) >= max_results:
        return None
    if rng() >= rate:
        return None
    chosen = {r.enzyme.id for r in dosed}
    if app is not None:
        # Jev 判定过:只在"可能适用"的假设里探索(p ≥ min_p),按 p 取最像有用的一条,
        # 探索名额不再浪费在与本请求无关的假设上。未被判定的候选不参与。
        results = sorted((r for r in results if app.p.get(r.enzyme.id, -1.0) >= min_p),
                         key=lambda r: app.p[r.enzyme.id], reverse=True)
    for r in results:
        if (r.enzyme.id not in chosen
                and r.enzyme.origin in _EXPLORATION_ORIGINS
                and r.enzyme.confidence < _EXPLORATION_MAX_CONF):
            return r
    return None


# ── Recall helpers ─────────────────────────────────────────────

async def _recall(
    search_query: str,
    context: str,
    pg: PGStore,
    qdrant: QdrantStore,
    domain: str | None,
    embedding_config: EmbeddingConfig | None = None,
    llm_config: LLMConfig | None = None,
    trace: dict | None = None,
    typesafe_config: TypeSafeConfig | None = None,
) -> list[Enzyme]:
    """Stage 2: Qdrant vector recall with PG keyword fallback.

    领域分流(recall.domain_route,见 app/matching/route.py):判官判断这次要用到哪些领域,与检索并行;
    expert 模式在全库召回之外再到选中的领域里各补一轮检索(补进来的另留名额,不挤占原有召回),并把选中领域的
    候选排到召回序前面、其余留在后面;strict 模式只留选中领域的候选。明确指定了 domain(CE_DOMAIN)时不分流。"""
    from app.llm_client import embed

    # 查询扩写与原查询 embedding 并行(见 app/matching/expand.py)
    expansion = (asyncio.create_task(expand_query(context, llm_config,
                                                  timeout=settings.get("recall.query_expand_timeout")))
                 if expand_on(llm_config) else None)
    # 词面召回(BM25)同样与 embedding 并行,不依赖向量(见 app/matching/lexical.py)
    lexical = (asyncio.create_task(qdrant.search_lexical(context, domain=domain, limit=lexical_limit()))
               if lexical_on() and hasattr(qdrant, "search_lexical") else None)
    # 领域分流同样与 embedding 并行:判官判断这次要用到哪些领域(见 app/matching/route.py)
    mode = route_mode()
    route_task = (asyncio.create_task(pick_domains(context, pg, typesafe_config))
                  if mode != "off" and not domain and typesafe_config is not None else None)

    # Try Qdrant vector search first
    vector = await embed(search_query, config=embedding_config)
    lex_hits = await _lexical_hits(lexical, trace)
    if expansion is not None and not vector:
        # embedding 不可用 → 扩写结果无处合并,取消掉:否则向量供应商出事时
        # (恰是最常见的路径)每个请求都白跑一次扩写 LLM,且异常无人回收
        expansion.cancel()
        expansion = None
    if vector or lex_hits:
        hits = await qdrant.search(vector, domain=domain, limit=settings.get("recall.dense_limit")) if vector else []
        if expansion is not None and vector:
            hits = await _merge_expansion(hits, expansion, qdrant, domain, embedding_config, trace)
        hits = merge_lexical(hits, lex_hits)
        ex_hits = await _exemplar_hits(qdrant, vector, domain) if vector else []
        ids, hints = _merge_exemplar_recall(hits, [h for h in ex_hits if h.get("kind", "pos") == "pos"])
        anti_hints = (_best_texts([h for h in ex_hits if h.get("kind") == "anti"])
                      if settings.get("exemplar.anti") else {})
        routed = await _route_extra(route_task, qdrant, vector, ids, trace, mode) if route_task else []
        route_task = None
        if ids:
            # 批量取酶(单条 IN 查询):get_enzymes 按 hit 顺序返回,保住召回序;
            # PG 侧 active 过滤是承重的第二道门(Qdrant payload 可能过期)。
            enzymes = await pg.get_enzymes(ids)
            recalled = [e for e in enzymes if e.status == "active"]
            if routed:
                # 专家通道:选中领域的候选排前面(召回序决定判定池里精排前列之外的名额给谁),其余照样留着;
                # 严格:只留选中领域的
                inside = [e for e in recalled if e.domain in routed]
                recalled = inside if mode == "strict" else inside + [e for e in recalled if e.domain not in routed]
            pos_hits = [h for h in ex_hits if h.get("kind", "pos") == "pos"]
            hint_scores = _best_scores(pos_hits)
            if trace is not None:   # 诊断:近邻经验投票(注入规则离线比过,不如类比识别,已不用于选择)
                trace["exemplar_votes"] = _exemplar_votes(pos_hits)
            for e in recalled:
                e._hint = hints.get(e.id, "")
                e._hint_score = hint_scores.get(e.id, 0.0)
                e._anti_hint = anti_hints.get(e.id, "")
            if recalled:
                return recalled

    if route_task is not None:
        route_task.cancel()
    # Fallback: PG keyword search
    logger.debug("Qdrant returned 0 hits, falling back to PG keyword search")
    keywords = _extract_keywords(context)
    if keywords:
        return await pg.keyword_search(keywords, domain=domain, limit=50)

    return []


# ── 经验样本召回(System 1 式识别)────────────────────────────────
# 记忆写的是抽象经验,情境是具体描述,两者在向量空间里常常离得远。每条记忆积累的
# 经验样本(被证实适用过的真实情境原文,Enzyme.exemplars)另建索引:当前情境与某条
# 记忆的过往情境相似,就等于"见过类似情况",把它召回并附上最像的那条作为提示。
_EXEMPLAR_LIMIT = 30      # 样本检索条数(同一酶可能多条,合并后取最高分)
# 合并后的召回上限:运行参数 recall.cap(默认 60 = 本体 50 + 样本带来的新增)


def _exemplar_hint_on() -> bool:
    """exemplar.hint(CE_EXEMPLAR_HINT):把最像的过往情境附给精排与 Jev(类比判断),默认 on。"""
    return settings.get("exemplar.hint")


def _exemplar_recall_on() -> bool:
    return settings.get("exemplar.recall")


async def _route_extra(task, qdrant, vector: list[float] | None, ids: list[str], trace: dict | None,
                       mode: str) -> list[str]:
    """等领域分流结果;选中了领域就到每个领域里补一轮向量检索,新候选追加在 ids 末尾(按分数),
    不占原有召回的名额。返回选中的领域名(没选中 / 超时 / 出错为空)。"""
    t0 = time.perf_counter()
    route = await await_route(task)
    routed = [name for name, _ in (route or {}).get("domains", [])]
    added: list[str] = []
    if routed and vector:
        expert = await asyncio.gather(*(qdrant.search(vector, domain=name, limit=settings.get("recall.domain_route_limit"))
                                        for name in routed))
        have, best = set(ids), {}
        for hits in expert:
            for h in hits:
                hid = h.get("id")
                if hid and hid not in have:
                    best[hid] = max(best.get(hid, -1.0), float(h.get("_score", 0.0)))
        added = sorted(best, key=best.get, reverse=True)
        ids.extend(added)
    if trace is not None:
        trace["route"] = {"mode": mode, "domains": (route or {}).get("domains", []),
                          "none": (route or {}).get("none"), "added_ids": added,
                          "status": "ok" if route is not None else "skipped",
                          "wait_ms": round((time.perf_counter() - t0) * 1000)}
    return routed


async def _lexical_hits(task, trace: dict | None) -> list[dict]:
    if task is None:
        return []
    try:
        hits = await task
    except Exception:
        logger.debug("lexical recall failed (non-fatal)", exc_info=True)
        hits = []
    if trace is not None:
        trace["lexical_ids"] = [h["id"] for h in hits if h.get("id")]
    return hits


async def _exemplar_hits(qdrant, vector: list[float], domain: str | None) -> list[dict]:
    search = getattr(qdrant, "search_exemplars", None)
    if search is None or not _exemplar_recall_on():
        return []
    try:
        return await search(vector, domain=domain, limit=_EXEMPLAR_LIMIT)
    except Exception:
        logger.debug("exemplar recall failed (non-fatal)", exc_info=True)
        return []


async def _merge_expansion(hits: list[dict], expansion, qdrant, domain, embedding_config, trace) -> list[dict]:
    """把扩写描述的检索结果并入原查询的召回。recall.query_expand_merge=max(默认,按相似度取最高)| rrf
    (只看两路名次)。真实基准 R@50(run_expand.py):原查询 63.4% → max 72.5% / rrf 71.5%,
    五个领域全部提升;单独用扩写在 Firefox 上更差(具体报告被抽象后丢细节),所以只能合并不能替换。"""
    from app.llm_client import embed
    try:
        text = await expansion
    except Exception:
        text = None
    if trace is not None:
        trace["expansion"] = (text or "")[:300]
    if not text:
        return hits
    vec = await embed(text, config=embedding_config)
    if not vec:
        return hits
    ex = await qdrant.search(vec, domain=domain, limit=50)
    if settings.get("recall.query_expand_merge") != "rrf":
        best: dict[str, dict] = {}
        for h in hits + ex:
            if h.get("id") and float(h.get("_score", 0)) > float(best.get(h["id"], {}).get("_score", -1)):
                best[h["id"]] = h
        return sorted(best.values(), key=lambda h: -float(h.get("_score", 0)))
    score: dict[str, float] = {}
    first: dict[str, dict] = {}
    for lst in (hits, ex):
        for rank, h in enumerate(lst):
            if h.get("id"):
                score[h["id"]] = score.get(h["id"], 0.0) + 1.0 / (60 + rank)
                first.setdefault(h["id"], h)
    # RRF 分落到 [0,1] 的相似度尺度上,保持与样本命中(余弦)合并时的可比性
    top = max(score.values(), default=1.0)
    return [{**first[i], "_score": score[i] / top} for i in sorted(score, key=score.get, reverse=True)]


_VOTE_NEIGHBORS = 10


def _exemplar_votes(ex_hits: list[dict]) -> dict[str, int]:
    """经验近邻投票:与当前情境最像的 10 条经验样本里,各记忆各占几条
    ("每次遇到这种情况用的都是它")。"""
    votes: dict[str, int] = {}
    for h in sorted(ex_hits, key=lambda h: -float(h.get("_score", 0)))[:_VOTE_NEIGHBORS]:
        if h.get("id"):
            votes[h["id"]] = votes.get(h["id"], 0) + 1
    return votes


def _best_scores(ex_hits: list[dict]) -> dict[str, float]:
    best: dict[str, float] = {}
    for h in ex_hits:
        if h.get("id"):
            best[h["id"]] = max(best.get(h["id"], -1.0), float(h.get("_score", 0.0)))
    return best


def _best_texts(ex_hits: list[dict]) -> dict[str, str]:
    """每条酶取分数最高的那条样本原文。"""
    best: dict[str, tuple[float, str]] = {}
    for h in ex_hits:
        eid, score = h.get("id"), float(h.get("_score", 0.0))
        if eid and score > best.get(eid, (-1.0, ""))[0]:
            best[eid] = (score, h.get("text", ""))
    return {eid: t for eid, (_, t) in best.items()}


def _merge_exemplar_recall(hits: list[dict], ex_hits: list[dict]) -> tuple[list[str], dict[str, str]]:
    """本体命中与样本命中按酶合并,召回分取两者最高;返回(按分排序的 id, 每条酶最像的样本)。"""
    best: dict[str, float] = {}
    for h in hits:
        if h.get("id"):
            best[h["id"]] = max(best.get(h["id"], -1.0), float(h.get("_score", 0.0)))
    hints: dict[str, str] = {}
    hint_score: dict[str, float] = {}
    for h in ex_hits:
        eid, score = h.get("id"), float(h.get("_score", 0.0))
        if not eid:
            continue
        best[eid] = max(best.get(eid, -1.0), score)
        if score > hint_score.get(eid, -1.0):
            hint_score[eid], hints[eid] = score, h.get("text", "")
    ids = sorted(best, key=best.get, reverse=True)[:settings.get("recall.cap")]
    return ids, hints


def _extract_keywords(text: str) -> list[str]:
    """Extract meaningful keywords from context text for PG fallback search."""
    keywords = []
    # CamelCase identifiers
    keywords += re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)+', text)
    # Dot-separated identifiers
    keywords += re.findall(r'[a-zA-Z]+\.[a-zA-Z]+(?:\.[a-zA-Z]+)*', text)
    # Chinese terms (2+ chars)
    keywords += re.findall(r'[\u4e00-\u9fff]{2,}', text)
    # ALL-CAPS acronyms (3+ chars)
    keywords += re.findall(r'\b[A-Z]{3,}\b', text)
    # Deduplicate, preserve order
    seen = set()
    unique = []
    for k in keywords:
        if k.lower() not in seen:
            seen.add(k.lower())
            unique.append(k)
    return unique[:10]

