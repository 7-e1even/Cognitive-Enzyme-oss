"""Core data models — Enzyme, Pattern, EvolutionEvent."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, PrivateAttr


# ── Enums ──────────────────────────────────────────────────────

class PatternType(str, Enum):
    KEYWORD = "keyword"
    REGEX = "regex"


class EnzymeType(str, Enum):
    REFLEX = "reflex"
    COMPOSITE = "composite"
    META = "meta"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Origin(str, Enum):
    MANUAL = "manual"
    EVOLVED = "evolved"
    HARVESTED = "harvested"
    REFINED = "refined"
    MUTATED = "mutated"
    CROSSED = "crossed"
    CONSOLIDATED = "consolidated"


class RelationType(str, Enum):
    CO_TRIGGERED = "co_triggered"
    PREREQUISITE = "prerequisite"
    GENERALIZATION = "generalization"
    EVOLVED_FROM = "evolved_from"
    SIMILAR = "similar"


class EventType(str, Enum):
    HEBBIAN = "hebbian"
    GENESIS = "genesis"
    HARVEST = "harvest"
    MUTATION = "mutation"
    CROSSOVER = "crossover"
    CONSOLIDATION = "consolidation"
    DREAM = "dream"
    IMMUNE = "immune"
    REPLAY = "replay"
    EXEMPLAR = "exemplar"


# ── Models ─────────────────────────────────────────────────────

class Pattern(BaseModel):
    type: PatternType = PatternType.KEYWORD
    value: str
    weight: float = 1.0


class Enzyme(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: EnzymeType = EnzymeType.REFLEX
    version: int = 1

    # 触发
    patterns: list[Pattern]
    conditions: list[str] = Field(default_factory=list)
    negations: list[str] = Field(default_factory=list)

    # 行动
    then: str
    because: str = ""
    severity: Severity = Severity.INFO
    references: list[str] = Field(default_factory=list)

    # 经验样本:该记忆在这些真实情境里被证实适用(情境原文)。召回时情境也与它们比对
    # ——见过类似情况就能认出来(System 1 式的识别),由反馈持续积累(Engine.learn_exemplar)。
    exemplars: list[str] = Field(default_factory=list)
    # 反例经验:推送后被证实不适用的情境。只作判定时的反向提示("上次类似情况这条不适用"),
    # 不参与召回;与 confidence(记忆本身好坏)分开——这里只教"认情境"。
    anti_exemplars: list[str] = Field(default_factory=list)

    # 突触强度
    confidence: float = 0.5
    precision: float = 0.5
    status: str = "active"         # active / quarantine / rejected
    quality_score: float = 0.0     # 0-5, from quality gate

    # 统计
    trigger_count: int = 0          # 被 report() 反馈过结果的次数（强信号）
    success_count: int = 0
    false_positive_count: int = 0
    last_triggered: datetime | None = None

    # 使用信号（与"结果反馈"分离）：prime() 命中即记录，无需依赖 report()
    activation_count: int = 0       # 被 prime() 命中并注入的次数（弱信号）
    last_activated: datetime | None = None

    # 谱系
    origin: Origin = Origin.MANUAL
    parent_ids: list[str] = Field(default_factory=list)

    # 分类
    domain: str = ""
    tags: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # 单次 prime 内的瞬态提示:召回时与当前情境最相似的那条经验样本(不持久化)
    _hint: str = PrivateAttr(default="")
    _anti_hint: str = PrivateAttr(default="")   # 最像的反例情境(瞬态)
    _hint_score: float = PrivateAttr(default=0.0)  # 最像的那条经验样本与当前情境的相似度(瞬态)

    @property
    def patterns_readable(self) -> str:
        """人类可读的模式描述。"""
        parts = []
        for p in self.patterns:
            prefix = "正则:" if p.type == PatternType.REGEX else ""
            parts.append(f"{prefix}{p.value}")
        return " / ".join(parts)

    @property
    def score(self) -> float:
        """综合排序分 = confidence × precision."""
        return self.confidence * max(self.precision, 0.1)


class Episode(BaseModel):
    """情景记忆 —— 一次会话中"发生了什么"的结构化骨架(情景层,酶库之下的冷层)。

    酶是提炼后的语义规则("该做什么"),情景是带出处的完整一幕("当时发生了什么")。
    回想 = 模式补全:拿手头的残缺线索(cues)与历史情景做重叠匹配,把整幕调回来。
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    domain: str = ""

    # 骨架(第一层,注入/回想时直接给)
    title: str                     # 一句话:这一幕是什么
    intent: str = ""               # 当时要做什么
    events: list[str] = Field(default_factory=list)   # 关键事件链(时间序)
    root_cause: str = ""           # 根因(如适用)
    outcome: str = ""              # 结局:解决了/绕过了/没解决
    lesson: str = ""               # 带走的教训

    # 回想把手(编码特异性:写入时存足线索)
    entities: list[str] = Field(default_factory=list)          # 涉及的文件/API/组件
    error_signatures: list[str] = Field(default_factory=list)  # 报错签名原文
    cues: list[str] = Field(default_factory=list)              # 归一化线索集(倒排索引键)

    # 细节(第二层,钻取时才给)
    excerpt: str = ""              # 原始对话关键片段(摘录,非总结)

    # 关联与权重
    enzyme_ids: list[str] = Field(default_factory=list)  # 本幕提炼/强化过的酶
    salience: float = 0.5

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EnzymeRelation(BaseModel):
    source_id: str
    target_id: str
    relation: RelationType
    weight: float = 1.0


class EvolutionEvent(BaseModel):
    enzyme_id: str
    event_type: EventType
    detail: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
