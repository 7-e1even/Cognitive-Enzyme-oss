"""Engine — orchestrator that ties storage, matching, and learning together."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from app import model_registry, settings
from app.config import Config
from app.distill.records import build_record, context_hash, decision_log_on, shadow_judges
from app.core import (
    Enzyme,
    EnzymeRelation,
    EvolutionEvent,
    EventType,
    Origin,
    Pattern,
    PatternType,
    RelationType,
)
from app.core.features import build_search_query, extract_keywords, scan_workspace
from app.learning.genesis import extract_patterns
from app.learning.hebbian import MAX_NEGATIONS, extract_negation
from app.matching.pipeline import MatchResult, run_pipeline
from app.storage.pg_store import ConcurrencyConflict, PGStore
from app.storage.qdrant_store import QdrantStore
from app.storage.sync import SyncManager

logger = logging.getLogger(__name__)

# 适用性感知的学分:prime 时记下 Jev 判定的适用概率 p,反馈到来时用它给失败加权——
# 被判"高度适用"却失败,说明是记忆内容本身有问题(该重罚);探索项/弱匹配失败只是
# 没对上(该轻罚)。进程内缓存,多进程拿不到就退回原逻辑。
_APPLICABILITY_TTL = 3600.0
_APPLICABILITY_MAX = 5000

# 经验样本(System 1 式识别):记忆在某个真实情境里被证实有用,就把这个情境记为它的
# 经验样本;召回时情境也与样本比对——见过类似情况就能认出来。prime 时缓存各条被注入
# 记忆的情境,反馈到来时取用(capture 常在会话结束才评估,TTL 取长一些)。
_CONTEXT_TTL = 6 * 3600.0
_EXEMPLAR_MIN_CHARS = 20
_EXEMPLAR_MAX_CHARS = 800


def _exemplar_learn_on() -> bool:
    return settings.get("exemplar.learn")


def _exemplar_max() -> int:
    return settings.get("exemplar.max")


def _anti_exemplar_on() -> bool:
    """exemplar.anti(CE_ANTI_EXEMPLAR):失败反馈是否记反例经验(默认 off)。真实法律经验流上反例
    没带来收益(命中 23.8%→22.8%、precision 62.1%→58.3%),验证有效前不默认开启。"""
    return settings.get("exemplar.anti")


def _anti_exemplar_max() -> int:
    return settings.get("exemplar.anti_max")


def normalize_exemplar(text: str) -> str:
    # 经验样本存的是用户情境原文(会进 PG、向量库、Dashboard):先脱敏再落盘,
    # 与 harvest/capture 写路径同一道门——prompt 里贴的 key/token 不能被"记住"
    from app.learning.privacy import scrub_secrets
    text = " ".join(scrub_secrets(text or "").split())
    return text[:_EXEMPLAR_MAX_CHARS]


def _grams(text: str) -> set[str]:
    t = text.lower()
    return {t[i:i + 3] for i in range(max(1, len(t) - 2))}


def _evict_redundant(items: list[str], cap: int) -> list[str]:
    """超出上限时淘汰"最冗余"的一条(与其余样本最像的那条,字符三元组 Jaccard),
    让有限的样本覆盖尽量多样的情境;彼此都不像(<0.3)时淘汰最旧的。"""
    items = list(items)
    while len(items) > cap:
        grams = [_grams(x) for x in items]
        best_i, best_sim = 0, -1.0
        for i in range(len(items)):
            sim = max((len(grams[i] & grams[j]) / (len(grams[i] | grams[j]) or 1)
                       for j in range(len(items)) if j != i), default=0.0)
            if sim > best_sim:
                best_i, best_sim = i, sim
        items.pop(best_i if best_sim >= 0.3 else 0)
    return items


def _jev_credit_on() -> bool:
    return settings.get("judge.credit")


def _hebbian_rule() -> str:
    """learning.hebbian_rule(CE_HEBBIAN_RULE): layered(默认,四档固定步长)| rw(Rescorla-Wagner 预测误差)。"""
    return settings.get("learning.hebbian_rule")


def failure_weight(applicability: float | None) -> float:
    """失败惩罚的倍率:p=0.3→0.95×,p=0.95→1.9×;无 p 时 1×(原逻辑)。"""
    if applicability is None:
        return 1.0
    return 0.5 + 1.5 * max(0.0, min(1.0, applicability))


class Engine:
    """Core engine — single entry point for all enzyme operations."""

    def __init__(self, config: Config, pg: PGStore, qdrant: QdrantStore):
        self.config = config
        self.pg = pg
        self.qdrant = qdrant
        self.sync = SyncManager(pg, qdrant)
        self._applicability: dict[str, tuple[float, float]] = {}  # id → (p, monotonic ts)
        self._contexts: dict[str, tuple[str, float]] = {}         # id → (prime 情境, monotonic ts)
        self._last_decision: dict[str, tuple[str, float]] = {}    # id → (最近一次推送它的判定 id, ts)
        self._bg: set = set()                                     # 判定记录等后台写入(防 GC)
        self._shadow_teachers: dict = {}                          # 影子判官实例缓存
        self._shadow_version = model_registry.version()           # 实例缓存对应的模型注册表版本

    # ── Matching ──────────────────────────────────────────

    async def match(
        self,
        context: str,
        domain: str | None = None,
        max_results: int = 15,
        features: dict | None = None,
        project: str = "",
        trace: dict | None = None,
        session_id: str = "",
        source: str = "prime",
    ) -> list[MatchResult]:
        """Run the full matching pipeline on a context string.

        判定记录开启时(distill.decision_log,默认开),这次判定的完整输入与决定在后台写入 decision_log,
        判定 id 放在 trace["decision_id"](评测据此回填标准答案)。"""
        query = build_search_query(context, features)
        record = decision_log_on()
        if record and trace is None:
            trace = {}
        results = await run_pipeline(
            context=context,
            search_query=query,
            pg=self.pg,
            qdrant=self.qdrant,
            domain=domain,
            max_results=max_results,
            embedding_config=self.config.embedding,
            project=project,
            trace=trace,
            typesafe_config=self.config.typesafe,
            llm_config=self.config.llm,
            expand_config=self.config.expand_llm,
            judge_llm_config=self.config.judge_llm,
        )
        self._remember_applicability(results)
        self._remember_contexts(context, results)
        if record:
            decision_id = uuid.uuid4().hex
            trace["decision_id"] = decision_id
            now = time.monotonic()
            for r in results:
                self._last_decision.pop(r.enzyme.id, None)
                self._last_decision[r.enzyme.id] = (decision_id, now)
            while len(self._last_decision) > _APPLICABILITY_MAX:
                self._last_decision.pop(next(iter(self._last_decision)))
            rec = build_record(decision_id, context, trace, results, session_id=session_id,
                               project=project, domain=domain or "")
            rec["policy"]["source"] = source
            self._background(self._persist_decision(rec))
            if rec["request"].get("questions"):
                for name in shadow_judges(source):
                    self._background(self._shadow_answer(name, decision_id, rec["request"]))
        return results

    async def _shadow_answer(self, name: str, decision_id: str, request: dict) -> None:
        """影子判官:同一道题原样交给候选学生 / 老师作答,只记录不影响决定(on-policy 对照数据)。"""
        try:
            if self._shadow_version != model_registry.version():   # 后台改了模型 → 重新实例化
                self._shadow_teachers.clear()
                self._shadow_version = model_registry.version()
            teacher = self._shadow_teachers.get(name)
            if teacher is None:
                from app.distill.teachers import load_teachers
                teacher = self._shadow_teachers[name] = load_teachers([name])[name]
            ans = await teacher.answer(request.get("state", ""), request.get("questions") or {})
            if ans.answers is not None:
                await self.pg.upsert_teacher_label(decision_id, name, ans.answers, ans.usage)
        except Exception:
            logger.debug("shadow judge %s failed (non-fatal)", name, exc_info=True)

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    spawn = _background   # 路由层的后台写入(判定记录等)也走这里,防 GC、不阻塞响应

    async def drain(self, timeout: float = 10.0) -> None:
        """关停前等后台写入(判定记录、事后结果、影子老师标注)落库,最多等 timeout 秒——
        否则最后几条训练数据会随连接池关闭丢掉。"""
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=timeout)

    async def _persist_decision(self, rec: dict) -> None:
        try:
            await self.pg.insert_decision(rec)
        except Exception:
            logger.debug("decision log write failed (non-fatal)", exc_info=True)

    def last_decision(self, enzyme_id: str) -> str:
        """最近一次推送这条记忆的判定 id(6 小时内),report 类反馈据此挂到判定上。"""
        hit = self._last_decision.get(enzyme_id)
        return hit[0] if hit and time.monotonic() - hit[1] <= _CONTEXT_TTL else ""

    async def record_outcome(self, enzyme_id: str, outcome: str, source: str = "", *,
                             decision_id: str = "", session_id: str = "", context: str = "") -> None:
        """事后结果(helped / contradicted / success / failure / revealed / gold),best-effort。"""
        if not decision_log_on() or not enzyme_id:
            return
        try:
            await self.pg.insert_outcome(enzyme_id, outcome, source, decision_id=decision_id,
                                         session_id=session_id,
                                         context_hash=context_hash(context) if context else "")
        except Exception:
            logger.debug("outcome log write failed (non-fatal)", exc_info=True)

    def _remember_contexts(self, context: str, results: list[MatchResult]) -> None:
        now = time.monotonic()
        for r in results:
            self._contexts.pop(r.enzyme.id, None)
            self._contexts[r.enzyme.id] = (context, now)
        while len(self._contexts) > _APPLICABILITY_MAX:
            self._contexts.pop(next(iter(self._contexts)))

    def recent_context(self, enzyme_id: str) -> str | None:
        hit = self._contexts.get(enzyme_id)
        if hit and time.monotonic() - hit[1] <= _CONTEXT_TTL:
            return hit[0]
        return None

    @staticmethod
    def _add_exemplar(enzyme: Enzyme, text: str, kind: str = "pos") -> bool:
        """把情境记为正例(适用)或反例(不适用)经验:过短/重复不记;同一情境以最新结果为准
        (从对立的一侧移除);超出上限淘汰最冗余的。返回是否新增。"""
        text = normalize_exemplar(text)
        if len(text) < _EXEMPLAR_MIN_CHARS:
            return False
        if kind == "pos":
            if text in enzyme.exemplars:
                return False
            enzyme.anti_exemplars = [x for x in enzyme.anti_exemplars if x != text]
            enzyme.exemplars = _evict_redundant(enzyme.exemplars + [text], _exemplar_max())
        else:
            if text in enzyme.anti_exemplars:
                return False
            enzyme.exemplars = [x for x in enzyme.exemplars if x != text]
            enzyme.anti_exemplars = _evict_redundant(enzyme.anti_exemplars + [text], _anti_exemplar_max())
        return True

    async def record_session_context(self, session_id: str, context: str) -> None:
        """prime 情境原文按会话短期留存(脱敏,best-effort):capture 事后发现某条记忆本该被想起时,
        从中挑出它真正适用的那一轮记为经验样本(见 app/learning/capture.py::_learn_from_session)。"""
        if not session_id or not context or not _exemplar_learn_on():
            return
        try:
            await self.pg.record_prime_context(session_id, normalize_exemplar(context))
        except Exception:
            logger.debug("record prime context failed (non-fatal)", exc_info=True)

    async def learn_exemplar(self, enzyme_id: str, context: str, source: str = "", kind: str = "pos") -> bool:
        """显式学一条经验(如 capture 归并印证:这次会话的情境本该想起这条记忆;
        kind="anti":这个情境下它被证实不适用)。乐观锁冲突时按 best-effort 放弃,
        下次会话还会再学。"""
        if not _exemplar_learn_on():
            return False
        enzyme = await self.pg.get_enzyme(enzyme_id)
        if not enzyme or not self._add_exemplar(enzyme, context, kind):
            return False
        read_version = enzyme.version
        enzyme.version += 1
        try:
            await self.update_enzyme(enzyme, expected_version=read_version)
        except ConcurrencyConflict:
            return False
        await self.pg.log_event(EvolutionEvent(
            enzyme_id=enzyme_id, event_type=EventType.EXEMPLAR,
            detail={"source": source, "kind": kind, "exemplar": normalize_exemplar(context)[:200],
                    "exemplars": len(enzyme.exemplars), "anti_exemplars": len(enzyme.anti_exemplars)},
        ))
        return True

    def _remember_applicability(self, results: list[MatchResult]) -> None:
        now = time.monotonic()
        for r in results:
            if r.applicability is not None:
                self._applicability.pop(r.enzyme.id, None)  # 重新插入 = 移到最新
                self._applicability[r.enzyme.id] = (r.applicability, now)
        while len(self._applicability) > _APPLICABILITY_MAX:
            self._applicability.pop(next(iter(self._applicability)))

    def recent_applicability(self, enzyme_id: str) -> float | None:
        hit = self._applicability.get(enzyme_id)
        if hit and time.monotonic() - hit[1] <= _APPLICABILITY_TTL:
            return hit[0]
        return None

    async def top_mature(
        self,
        min_confidence: float = 0.75,
        min_precision: float = 0.7,
        limit: int = 10,
    ) -> list[Enzyme]:
        return await self.pg.top_mature(min_confidence, min_precision, limit)

    # ── CRUD ──────────────────────────────────────────────

    async def create_enzyme(
        self,
        patterns: list[Pattern],
        then_action: str,
        because: str = "",
        origin: Origin = Origin.MANUAL,
        confidence: float = 0.5,
        domain: str = "",
        tags: list[str] | None = None,
        references: list[str] | None = None,
        severity: str = "info",
        parent_ids: list[str] | None = None,
        status: str = "active",
    ) -> Enzyme:
        """Create a new enzyme and sync to Qdrant.

        ``status`` 允许创建即隔离(harvest 质量门 QUARANTINE):先建 active 再改
        状态会在两次写入之间留一个可被 match 注入的窗口。"""
        enzyme = Enzyme(
            patterns=patterns,
            then=then_action,
            because=because,
            origin=origin,
            confidence=confidence,
            domain=domain,
            tags=tags or [],
            references=references or [],
            severity=severity,
            parent_ids=parent_ids or [],
            status=status,
        )
        await self.pg.upsert_enzyme(enzyme)
        await self.sync.sync_enzyme(enzyme)
        logger.info("Created enzyme %s: %s", enzyme.id, enzyme.then[:60])
        return enzyme

    async def get_enzyme(self, enzyme_id: str) -> Enzyme | None:
        return await self.pg.get_enzyme(enzyme_id)

    async def update_enzyme(self, enzyme: Enzyme, expected_version: int | None = None) -> None:
        """全行更新并同步向量。``expected_version`` 非空时乐观锁更新
        (读取时的版本号),冲突抛 ConcurrencyConflict——hebbian 反馈等
        并发读-改-写路径用它防丢更新;管理后台编辑等低频路径不传即可。"""
        enzyme.updated_at = datetime.now(timezone.utc)
        generation = await self.pg.upsert_enzyme(enzyme, expected_version=expected_version)
        await self.sync.sync_enzyme(enzyme, generation)

    async def delete_enzyme(self, enzyme_id: str) -> bool:
        ok = await self.pg.delete_enzyme(enzyme_id)
        if ok:
            await self.sync.sync_delete(enzyme_id)
        return ok

    # ── Learning ──────────────────────────────────────────

    async def hebbian_update(
        self,
        enzyme_id: str,
        success: bool,
        expected: bool | None = None,
        detail: str = "",
        credit: float = 1.0,
        update_stats: bool = True,
    ) -> dict:
        """Apply Hebbian learning with prediction error layering.

        ``credit`` (0–1) scales the confidence delta — used by report_batch to
        share one outcome across co-suggested enzymes (responsibility-weighted
        credit assignment). ``update_stats=False`` applies only a confidence
        nudge without touching trigger/precision counters, so a faint "echo" to
        a co-activated enzyme doesn't pollute its precision denominator.

        这是读-改-写的并发热点(/report、capture 任务、replay 可同时打同一条记忆):
        用 version 乐观锁,冲突时重读重算,最多 3 次,不再静默互相覆盖。
        """
        for attempt in range(3):
            try:
                return await self._hebbian_update_once(
                    enzyme_id, success, expected=expected, detail=detail,
                    credit=credit, update_stats=update_stats,
                )
            except ConcurrencyConflict:
                if attempt == 2:
                    # 连冲 3 次说明争用激烈;按失败处理返回错误,调用方都识别 "error" 键
                    logger.warning("hebbian_update %s: 3 次乐观锁冲突,放弃本轮更新", enzyme_id)
                    return {"error": "concurrency conflict", "enzyme_id": enzyme_id}
                await asyncio.sleep(0.05 * (attempt + 1))
        return {"error": "concurrency conflict"}  # pragma: no cover

    async def _hebbian_update_once(
        self,
        enzyme_id: str,
        success: bool,
        expected: bool | None = None,
        detail: str = "",
        credit: float = 1.0,
        update_stats: bool = True,
    ) -> dict:
        enzyme = await self.pg.get_enzyme(enzyme_id)
        if not enzyme:
            return {"error": "enzyme not found"}
        read_version = enzyme.version

        # Auto-infer expected
        if expected is None:
            expected = enzyme.confidence > 0.7

        before = {"confidence": enzyme.confidence, "precision": enzyme.precision}

        # Prediction error layering
        genesis_triggered = False
        if success and expected:
            delta = +0.02       # 维持性学习
        elif success and not expected:
            delta = +0.10       # 探索性学习
            genesis_triggered = True
        elif not success and expected:
            delta = -0.15       # 纠错性学习
        else:
            delta = -0.05       # 确认性削弱

        delta *= max(0.0, min(1.0, credit))
        applicability = self.recent_applicability(enzyme_id) if _jev_credit_on() else None
        if _hebbian_rule() == "rw":
            # Rescorla-Wagner:Δ = α·w·(结果 − 当前 confidence)。更新幅度与"意外程度"成正比;
            # 失败按注入时的适用概率 p 加权(推得准却失败 = 记忆本身有问题),成功权重 1。
            w = 1.0 if success else (applicability if applicability is not None else 1.0)
            target = 1.0 if success else 0.0
            delta = (settings.get("learning.hebbian_alpha") * w * (target - enzyme.confidence)
                     * max(0.0, min(1.0, credit)))
        elif not success:
            delta *= failure_weight(applicability)
        enzyme.confidence = max(0.0, min(1.0, enzyme.confidence + delta))

        # Update outcome stats (skipped for low-credit "echo" updates)
        if update_stats:
            enzyme.trigger_count += 1
            if success:
                enzyme.success_count += 1
            else:
                enzyme.false_positive_count += 1
            if enzyme.trigger_count > 0:
                enzyme.precision = enzyme.success_count / enzyme.trigger_count
            enzyme.last_triggered = datetime.now(timezone.utc)

        # 主反馈 → 把注入时的情境记为经验:成功记正例(下次类似情境更容易想起),
        # 失败记反例(下次类似情境提醒判定"上次这条不适用")
        learned_exemplar = False
        if update_stats and _exemplar_learn_on() and (success or _anti_exemplar_on()):
            ctx = self.recent_context(enzyme_id)
            if ctx:
                learned_exemplar = self._add_exemplar(enzyme, ctx, "pos" if success else "anti")

        # Negative learning: on corrective feedback, try to learn a suppressor
        learned_negation = None
        if update_stats and (not success and expected) and detail:
            try:
                learned_negation = await extract_negation(enzyme, detail, self.config.llm)
            except Exception:
                logger.exception("extract_negation failed (non-fatal)")
            if learned_negation:
                enzyme.negations = (enzyme.negations + [learned_negation])[-MAX_NEGATIONS:]

        enzyme.version += 1
        await self.update_enzyme(enzyme, expected_version=read_version)

        # Log evolution
        await self.pg.log_event(EvolutionEvent(
            enzyme_id=enzyme_id,
            event_type=EventType.HEBBIAN,
            detail={
                "before": before,
                "after": {"confidence": enzyme.confidence, "precision": enzyme.precision},
                "delta": delta,
                "success": success,
                "expected": expected,
                "credit": credit,
                "applicability": applicability,
                "learned_exemplar": learned_exemplar,
                "detail": detail,
                "learned_negation": learned_negation,
            },
        ))

        result = {
            "enzyme_id": enzyme_id,
            "delta": round(delta, 4),
            "confidence": round(enzyme.confidence, 3),
            "precision": round(enzyme.precision, 3),
            "learning_type": (
                "维持" if success and expected else
                "探索" if success and not expected else
                "纠错" if not success and expected else
                "确认削弱"
            ),
        }
        if learned_exemplar:
            result["learned_exemplar"] = True
        if learned_negation:
            result["learned_negation"] = learned_negation
            result["tip_negation"] = f"已学会抑制条件『{learned_negation}』，今后该上下文不再触发此酶。"

        # Genesis on unexpected success
        if genesis_triggered:
            result["genesis"] = "unexpected success → consider creating new focused enzyme"

        return result

    async def report_batch(
        self,
        enzyme_ids: list[str],
        success: bool,
        detail: str = "",
        expected: bool | None = None,
    ) -> dict:
        """Report a whole prime() batch outcome with responsibility-weighted credit.

        ``enzyme_ids`` is the ranked order returned by prime(). The top-ranked
        enzyme is the primary and receives full credit + stat update; the rest
        receive an exponentially-decaying confidence echo (no stat pollution).
        This solves the N→1 attribution problem: the agent confirms the batch
        once instead of guessing which single suggestion mattered.

        ``expected`` is forwarded to the primary's Hebbian update: pass ``False``
        to flag an unexpected outcome (drives exploratory learning / genesis);
        ``None`` (default) lets the engine auto-infer from confidence.
        """
        if not enzyme_ids:
            return {"error": "no enzyme_ids"}

        results = []
        # Primary: full credit + stats (+ negation learning on failure)
        primary = await self.hebbian_update(
            enzyme_ids[0], success, expected=expected, detail=detail, credit=1.0, update_stats=True,
        )
        results.append(primary)

        # Echoes: rank-decayed confidence-only nudges to co-suggested enzymes
        for i, eid in enumerate(enzyme_ids[1:], start=1):
            credit = 0.5 ** i  # rank 1 → 0.5, rank 2 → 0.25, ...
            if credit < 0.05:
                break
            r = await self.hebbian_update(
                eid, success, detail="", credit=credit, update_stats=False,
            )
            results.append(r)

        for i, eid in enumerate(enzyme_ids[:len(results)]):
            await self.record_outcome(eid, "success" if success else "failure",
                                      "report_batch" if i == 0 else "report_batch_echo",
                                      decision_id=self.last_decision(eid))

        return {
            "reported": len([r for r in results if "error" not in r]),
            "primary": enzyme_ids[0],
            "success": success,
            "updates": results,
        }

    async def genesis(
        self,
        parent_enzyme_id: str,
        insight: str,
        context: str,
        domain: str = "",
    ) -> Enzyme:
        """Generate a new enzyme from unexpected outcome."""
        from app.learning.genesis import extract_patterns_llm
        text = insight + " " + context
        patterns = await extract_patterns_llm(text)
        if not patterns:
            patterns = await extract_patterns(text, pg=self.pg)
        if not patterns:
            patterns = [Pattern(value=insight[:50])]

        new_enzyme = await self.create_enzyme(
            patterns=patterns,
            then_action=insight,
            because=f"从意外结果中发现: {context[:100]}",
            origin=Origin.EVOLVED,
            confidence=0.3,
            domain=domain,
            parent_ids=[parent_enzyme_id],
        )

        # Create evolved_from relation
        await self.pg.upsert_relation(EnzymeRelation(
            source_id=new_enzyme.id,
            target_id=parent_enzyme_id,
            relation=RelationType.EVOLVED_FROM,
        ))

        await self.pg.log_event(EvolutionEvent(
            enzyme_id=new_enzyme.id,
            event_type=EventType.GENESIS,
            detail={"parent": parent_enzyme_id, "insight": insight, "context": context[:200]},
        ))

        logger.info("Genesis: %s → %s", parent_enzyme_id, new_enzyme.id)
        return new_enzyme

    async def harvest(
        self,
        insight: str,
        context: str,
        domain: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        """AI proactively contributes learned experience as new enzyme.

        Runs quality gate before storage:
          known     → 模型闭卷已会,不存
          pass      → store with mapped confidence
          refine    → store LLM-improved version
          quarantine → store but exclude from prime()
          reject    → don't store, return suggestions
        """
        from app.learning import known_filter
        from app.learning.genesis import extract_patterns_llm
        from app.learning.privacy import scrub_secrets
        from app.learning.quality_gate import assess_quality, PASS, REFINE, QUARANTINE, REJECT

        # 0. 隐私脱敏 — REST /harvest、MCP harvest、capture ADD 都经过这里
        insight = scrub_secrets(insight)
        context = scrub_secrets(context)

        # 0.5 已会过滤 — 模型闭卷就会的经验不撞库不入库(learning.skip_known)
        if known_filter.enabled():
            covered, reason = await known_filter.judge({"trigger": context, "then": insight})
            if covered:
                logger.info("Harvest SKIPPED (model already knows): %s", reason)
                return {"status": "known", "reason": reason,
                        "message": "模型闭卷已经会这条经验,未存入。"}

        # 1. Dedup: find similar existing enzymes (vector search, include quarantine)
        similar_raw = []
        try:
            from app.llm_client import embed
            text = f"{insight} {context[:200]}"
            vector = await embed(text, config=self.config.embedding)
            if vector:
                hits = await self.qdrant.search(vector, limit=5, include_all=True)
                similar_raw = [h for h in hits if isinstance(h, dict)]
        except Exception as e:
            logger.debug("Dedup search failed (non-fatal): %s", e)

        # 2. Quality gate
        quality = await assess_quality(insight, context, similar_raw, typesafe_config=self.config.typesafe)

        # 3. Decision
        if quality.decision == REJECT:
            logger.info("Harvest REJECTED (score=%.2f): %s", quality.score, quality.reason)
            return {
                "status": "rejected",
                "quality_score": quality.score,
                "reason": quality.reason,
                "suggestions": quality.suggestions,
                "message": "质量不达标，未存入。请参考建议改进后重试。",
            }

        # Determine the actual insight to store
        final_insight = insight
        origin = Origin.HARVESTED
        if quality.decision == REFINE and quality.improved_insight:
            final_insight = quality.improved_insight
            origin = Origin.REFINED

        # Map quality score to confidence: score 3.5-5.0 → conf 0.35-0.55
        confidence = max(0.15, min(0.55, quality.score * 0.11))

        status = "active"
        if quality.decision == QUARANTINE:
            status = "quarantine"
            confidence = 0.15  # Minimal confidence for quarantined

        # 4. Extract patterns from the (possibly refined) insight
        text = final_insight + " " + context
        patterns = await extract_patterns_llm(text)
        if not patterns:
            patterns = await extract_patterns(text, pg=self.pg)
        if not patterns:
            words = [w for w in final_insight.split() if len(w) > 2][:3]
            patterns = [Pattern(value=w) for w in words] if words else [Pattern(value=final_insight[:30])]

        # 4.5 领域:判官归入已有领域,都不合适时由语言模型新建(app/learning/domains.py);失败沿用传入的领域
        domain_note: dict = {}
        try:
            from app.learning.domains import resolve_domain
            domain, domain_note = await resolve_domain(self.pg, final_insight, context, domain, self.config.typesafe)
        except Exception as e:
            logger.warning("Domain auto-classification failed, keeping given domain: %s", e)

        # 5. Create enzyme(隔离件创建即 quarantine,不留 active 窗口)
        enzyme = await self.create_enzyme(
            patterns=patterns,
            then_action=final_insight,
            because=f"从实际任务中发现: {context[:200]}",
            origin=origin,
            confidence=confidence,
            domain=domain,
            tags=tags or [],
            status=status,
        )

        # Set quality fields
        enzyme.quality_score = quality.score
        # 诞生情境即第一条经验样本:第一次经历就形成识别模板,下次遇到类似情境立刻能认出来
        if _exemplar_learn_on() and context:
            self._add_exemplar(enzyme, context)
        await self.update_enzyme(enzyme)

        await self.pg.log_event(EvolutionEvent(
            enzyme_id=enzyme.id,
            event_type=EventType.HARVEST,
            detail={
                "insight": insight,
                "final_insight": final_insight,
                "context": context[:200],
                "quality": {
                    "decision": quality.decision,
                    "score": quality.score,
                    "dimensions": quality.dimensions,
                },
                "domain": {"name": domain, **domain_note},
            },
        ))

        logger.info("Harvest %s (decision=%s score=%.2f): %s",
                     enzyme.id, quality.decision, quality.score, enzyme.then[:60])

        result = {
            "id": enzyme.id,
            "status": status,
            "quality_score": quality.score,
            "confidence": round(enzyme.confidence, 3),
            "decision": quality.decision,
            "domain": domain,
            "message": {
                PASS: "质量通过，已存入。",
                REFINE: "已自动改进后存入。",
                QUARANTINE: "质量待验证，已隔离存入。在 dashboard 审核后可激活。",
            }.get(quality.decision, "已存入。"),
        }
        if quality.suggestions:
            result["suggestions"] = quality.suggestions
        if quality.decision == REFINE:
            result["original_insight"] = insight
            result["refined_insight"] = final_insight

        return result

    # ── Usage tracking ────────────────────────────────────

    async def record_activations(self, enzyme_ids: list[str]) -> None:
        """Record that prime() matched & injected these enzymes.

        This closes the feedback loop's dense half: every prime() hit is
        evidence the enzyme was *used*, independent of whether the agent later
        calls report(). Consolidation/immune use this as the real "is this
        enzyme alive" signal instead of the sparse, easily-starved trigger_count.
        """
        if not enzyme_ids:
            return
        try:
            await self.pg.record_activations(enzyme_ids)
        except Exception:
            logger.exception("record_activations failed (non-fatal)")

    # ── Co-trigger tracking ───────────────────────────────

    async def record_co_triggers(self, enzyme_ids: list[str]) -> None:
        """Record co-triggered enzymes in the same session (批量单事务,weight 累计共现次数)。"""
        pairs: list[tuple[str, str]] = []
        for i, a in enumerate(enzyme_ids):
            for b in enzyme_ids[i + 1:]:
                pairs.append((a, b))
                pairs.append((b, a))
        try:
            await self.pg.upsert_co_trigger_pairs(pairs)
        except Exception:
            logger.exception("record_co_triggers failed (non-fatal)")
