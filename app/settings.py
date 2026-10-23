"""运行参数注册表——所有可调参数的唯一出处:类型、默认值、取值范围、说明与分组。

值存数据库(``app_settings`` 表,键 ``cfg:<key>``,JSON 编码),后台「参数配置」页修改后即时生效。
取值顺序:后台保存的值 > 环境变量(兼容旧部署;评测脚本按环境变量做 A/B)> 默认值。

后台值只由服务进程在启动时 :func:`load` 载入进程内缓存,修改接口同步更新,app/runtime.watch 定时
与其他副本对齐。评测脚本与离线工具不调 ``load()``,行为与改造前一致:只看环境变量与默认值。

新增参数:在 ``SPECS`` 里加一条,代码里用 ``settings.get("<key>")`` 读取,后台页面自动出现。
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

logger = logging.getLogger(__name__)

PREFIX = "cfg:"

TYPES = ("bool", "int", "float", "enum", "str", "list", "json", "model", "models")


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    description: str


@dataclass(frozen=True)
class Spec:
    key: str
    type: str
    default: Any
    label: str
    help: str = ""
    env: str = ""                                  # 兼容的环境变量名
    choices: tuple[tuple[str, str], ...] = ()      # enum:(取值, 说明)
    min: float | None = None
    max: float | None = None
    unit: str = ""
    restart: bool = False                          # 改动需重启服务才生效
    advanced: bool = False                         # 后台默认折叠
    kinds: tuple[str, ...] = ()                    # model / models:允许绑定的模型类型
    env_floor: bool = False                        # 环境变量也按 min 截断(改造前代码里就有 max(1, …) 的参数)

    @property
    def group(self) -> str:
        return self.key.split(".", 1)[0]


class SettingError(ValueError):
    def __init__(self, key: str, message: str):
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


GROUPS: tuple[Group, ...] = (
    Group("model", "模型绑定", "各环节使用哪个模型;服务商与模型在「模型」页管理。未绑定的环节沿用环境变量里的旧配置。"),
    Group("recall", "召回", "向量召回之外的补充通道:领域分流、词面召回(BM25)与查询扩写。"),
    Group("ranking", "精排与排序", "精排调用、相关性下限、打分权重、剂量与探索。"),
    Group("judge", "适用性判定", "Stage 4.5:由 System One 判官(如 Jev)决定注入哪几条;失败或关闭时回退固定剂量。"),
    Group("selection", "选择阈值", "判官概率 → 注入决定的规则。默认值经留一领域交叉验证选定,按 jev-1.13.0 标定;换判官模型后需重新标定。"),
    Group("exemplar", "经验与类比", "记忆的过往适用情境(经验样本):学习、召回、提示与类比识别。"),
    Group("learning", "学习", "反馈学分规则、入库前的已会过滤与巩固时的语义去重。"),
    Group("domain", "领域", "新记忆入库时自动归类:判官归入已有领域,都不合适时由语言模型新建。"),
    Group("distill", "判定记录", "判定记录、归档与影子判官——为训练自己的 System 1 判官积累数据。"),
    Group("gateway", "System 1 网关", "POST /api/v1/systemone 的判官路由与置信门槛。"),
    Group("system", "系统", "向量库连接、规则文件与工作目录。"),
)

_BOOL_TRUE = {"1", "true", "on", "yes", "y"}
_BOOL_FALSE = {"0", "false", "off", "no", "n"}


def _f(key, default, label, help="", env="", **kw) -> Spec:
    return Spec(key, "float", default, label, help, env, **kw)


def _i(key, default, label, help="", env="", **kw) -> Spec:
    return Spec(key, "int", default, label, help, env, **kw)


def _b(key, default, label, help="", env="", **kw) -> Spec:
    return Spec(key, "bool", default, label, help, env, **kw)


def _e(key, default, label, choices, help="", env="", **kw) -> Spec:
    return Spec(key, "enum", default, label, help, env, choices=choices, **kw)


SPECS_LIST: tuple[Spec, ...] = (
    # ── 模型绑定(取值为「模型」页里的模型注册名)──────────────────────
    Spec("model.llm", "model", "", "引擎 LLM",
         "capture 提炼、被动评估、进化(梦境 / 突变 / 交叉)与情景回放判卷。", kinds=("chat",)),
    Spec("model.embedding", "model", "", "向量模型",
         "记忆与情境的向量化。换模型后需在「系统」页重建向量索引;向量维度变化需重启服务。",
         kinds=("embedding",)),
    Spec("model.rerank", "model", "", "精排模型", "Stage 3.5 语义精排(/rerank 接口)。", kinds=("rerank",)),
    Spec("model.judge", "model", "", "适用性判官",
         "Stage 4.5 适用性判定与巩固时的语义去重(System One 协议,如 Jev)。", kinds=("judge",)),
    Spec("model.query_expand", "model", "", "查询扩写模型", "开启查询扩写时使用;留空用引擎 LLM。",
         kinds=("chat",), advanced=True),
    Spec("model.judge_llm", "model", "", "第二判官模型", "开启 LLM 第二判官时使用;留空用引擎 LLM。",
         kinds=("chat",), advanced=True),

    # ── 召回 ───────────────────────────────────────────────────────
    _b("recall.lexical", True, "词面召回(BM25)",
       "把 BM25 命中并入向量召回:具体情境与抽象记忆常常只共享少数罕见词(CWE 编号、法律术语、组件名)。"
       "真实基准上候选池覆盖 65.9% → 74.1%,两套评测栈 F1 均提升。", "CE_LEXICAL_RECALL"),
    _i("recall.lexical_limit", 10, "词面召回条数", "并入召回的 BM25 命中数。", "CE_LEXICAL_LIMIT", min=1, max=100, env_floor=True),
    _i("recall.dense_limit", 50, "向量召回条数", "向量检索取前多少条。", "CE_DENSE_LIMIT", min=10, max=300,
       advanced=True, env_floor=True),
    _i("recall.cap", 60, "召回合并上限", "向量、词面与经验样本命中合并后最多保留几条。", "CE_RECALL_CAP",
       min=10, max=400, advanced=True, env_floor=True),
    _f("recall.lexical_avgdl", 60.0, "BM25 平均文档长度", "文档侧归一化参数,只影响之后写入的词面索引。",
       "CE_LEXICAL_AVGDL", min=1, advanced=True, env_floor=True),
    _b("recall.query_expand", False, "查询扩写",
       "用 LLM 把具体情境改写成抽象描述再检索一次,与原查询合并(真实基准 R@50 63.4% → 72.5%);"
       "每次 prime 多一次 LLM 调用。", "CE_QUERY_EXPAND"),
    _e("recall.query_expand_merge", "max", "扩写合并方式",
       (("max", "按相似度取最高"), ("rrf", "名次融合(RRF)")), "", "CE_QUERY_EXPAND_MERGE", advanced=True),
    _f("recall.query_expand_timeout", 8.0, "扩写超时", "", "CE_QUERY_EXPAND_TIMEOUT", min=0.5, unit="秒",
       advanced=True),
    _e("recall.domain_route", "expert", "领域分流",
       (("off", "关闭"), ("expert", "专家通道:全库照常召回,再到选中的领域里补召回,选中领域的候选排前面"),
        ("strict", "严格:只在选中的领域里召回")),
       "提问时判官判断这次要用到哪些领域(与召回并行);判官不可用、超时或判为通用问题时照常全库召回。"
       "测试平台 250 条真实提问:分流选对领域 93%,判定池覆盖 181→190(严格 183,Firefox 掉 7 条);"
       "判定池有变化的 9 条注入命中 0→5、误注入 3→1。", "CE_DOMAIN_ROUTE"),
    _f("recall.domain_route_q", 0.3, "分流门槛", "领域的单选概率 ≥ 此值且高于「都不需要」才选中。", "CE_DOMAIN_ROUTE_Q",
       min=0, max=1),
    _i("recall.domain_route_k", 2, "最多选几个领域", "", "CE_DOMAIN_ROUTE_K", min=1, max=5),
    _i("recall.domain_route_limit", 20, "每个领域补召回条数", "", "CE_DOMAIN_ROUTE_LIMIT", min=1, max=100,
       advanced=True),
    _f("recall.domain_route_timeout", 2.0, "分流等待上限", "向量检索完成后最多再等判官这么久,超时照常全库召回。",
       "CE_DOMAIN_ROUTE_TIMEOUT", min=0.2, unit="秒", advanced=True),

    # ── 精排与排序 ─────────────────────────────────────────────────
    _f("ranking.relevance_floor", 0.01, "相关性下限",
       "精排分低于此值的候选直接淘汰——静默纪律的主要来源(bge-reranker 不相关 ~1e-4,相关 >0.1)。0 关闭。",
       "CE_RELEVANCE_FLOOR", min=0, max=1),
    _f("ranking.rerank_topn_mult", 2.0, "精排保留倍数", "精排 top_n = 请求条数 × 此倍数。",
       "CE_RERANK_TOPN_MULT", min=1, max=10),
    _e("ranking.rerank_keep", "topn", "精排保留方式",
       (("topn", "按条数:只留 top_n"), ("floor", "按分数:相关性下限以上全部保留")),
       "按条数会截掉分数很高、只是名次靠后的候选。", "CE_RERANK_KEEP", advanced=True),
    _f("ranking.rerank_timeout", 15.0, "精排超时", "", "CE_RERANK_TIMEOUT", min=1, unit="秒"),
    _i("ranking.rerank_retries", 1, "精排重试次数",
       "429 / 5xx / 超时时重试(指数退避,遵守 Retry-After)。热路径保持默认,批量评测撞限流时调大。",
       "CE_RERANK_RETRIES", min=0, max=10, env_floor=True),
    _f("ranking.rerank_backoff_cap", 2.0, "精排退避上限", "单次退避最长等待。", "CE_RERANK_BACKOFF_CAP",
       min=0, unit="秒", advanced=True, env_floor=True),
    _e("ranking.conditions_mode", "soft", "适用条件模式",
       (("soft", "软:命中加分,不淘汰"), ("hard", "硬:未全部命中即淘汰")),
       "评测中硬模式的淘汰 11/11 都是误杀,软模式召回 +8pt、精度 +4.3pt。", "CE_CONDITIONS_MODE"),
    _f("ranking.conditions_boost", 0.2, "条件命中加成", "软模式下条件命中时给模式分的加成。",
       "CE_CONDITIONS_BOOST", min=0, max=1, advanced=True),
    _e("ranking.conditions_boost_stage", "pattern", "加成生效阶段",
       (("pattern", "模式匹配阶段"), ("rerank", "精排之后再补一次")), "", "CE_CONDITIONS_BOOST_STAGE",
       advanced=True),
    _f("ranking.w_relevance", 0.55, "权重 · 相关性", "精排 / 模式匹配分。", "CE_W_RELEVANCE", min=0, max=1),
    _f("ranking.w_importance", 0.15, "权重 · 重要性", "记忆置信度。", "CE_W_IMPORTANCE", min=0, max=1),
    _f("ranking.w_return", 0.10, "权重 · 回报", "历史精度(成功率)。", "CE_W_RETURN", min=0, max=1),
    _f("ranking.w_recency", 0.15, "权重 · 新近度", "距上次使用的时间衰减。", "CE_W_RECENCY", min=0, max=1),
    _f("ranking.w_credibility", 0.05, "权重 · 可信度", "使用次数带来的可信度。", "CE_W_CREDIBILITY", min=0, max=1),
    _i("ranking.dosage_budget", 5, "剂量预算", "判官不可用时的固定剂量:没有危急项时最多注入几条。",
       "CE_DOSAGE_BUDGET", min=1, max=20),
    _f("ranking.exploration_rate", 0.2, "探索槽概率",
       "以此概率在剂量外追加一条未验证的假设记忆(梦境 / 突变 / 交叉产出),让它进入反馈循环。",
       "CE_EXPLORATION_RATE", min=0, max=1),
    _f("ranking.refractory_seconds", 600.0, "不应期",
       "同一会话内同一条记忆注入后,窗口期内不再重复注入(危急级除外)。", "CE_REFRACTORY_SECONDS",
       min=0, unit="秒"),

    # ── 适用性判定 ─────────────────────────────────────────────────
    _b("judge.enabled", True, "启用适用性判定",
       "绑定了判官模型时生效:按判官给的概率决定注入哪几条、注入几条,替代固定剂量。"
       "黄金集 A/B:召回 68% → 87%,精度 34% → 96%,静默 100%。", "CE_JEV_MODE"),
    _e("judge.stage", "post_rerank", "判定时机",
       (("post_rerank", "精排之后(更准)"), ("parallel", "与精排并行(省约 0.5 秒)")), "", "CE_JEV_STAGE"),
    _i("judge.pool", 36, "判定池大小", "每次 prime 判定的候选上限(24 / 36 / 50 → 召回 86% / 88% / 85%)。",
       "CE_JEV_POOL", min=1, max=100, env_floor=True),
    _e("judge.pool_order", "scored", "判定池组成",
       (("scored", "综合分前列 + 召回序补足"), ("rrf", "召回序与精排序名次融合(RRF)")),
       "决定哪些候选进判定池。数学/物理冻结库 146 个正例:池 36 时综合分 96 个、RRF 99 个,池 48 时 97 / 104 个;"
       "只按精排排反而更少(81 个)。RRF 需要精排给全部候选打分(ranking.rerank_keep=floor)。",
       "CE_JEV_POOL_ORDER", advanced=True),
    _f("judge.timeout", 6.0, "判定超时", "超时回退固定剂量。", "CE_JEV_TIMEOUT", min=0.5, unit="秒"),
    _i("judge.token_budget", 26000, "判官请求预算",
       "判官单次请求的估算 token 上限(bocha-jev-v1 硬上限 32768,超出直接 422 判定作废)。"
       "超出时按精排顺序截掉靠后的候选;0 = 不限制。", "CE_JEV_TOKEN_BUDGET", min=0, advanced=True),
    _b("judge.two_stage", False, "两段判定",
       "先分批逐条扫判定池,再比较原单批候选与扫描高分候选,复用扫描分数、不重复逐条判断。"
       "用于覆盖被单请求预算裁掉的候选;代价是每次 prime 多次判官调用。", "CE_JEV_TWO_STAGE"),
    _e("judge.two_stage_compare", "protect", "两段判定 · 比较方式",
       (("protect", "保护原单批候选,新增候选另做比较"),
        ("joint", "全部扫完后按逐条概率取前 compare_k 条一起比较(含类比题)")),
       "仅在 judge.two_stage 开启时生效。", "CE_JEV_TWO_STAGE_COMPARE", advanced=True),
    _b("judge.scan_reuse_fit", False, "复用原池判定",
       "两段判定先复用原单批候选的 p/q,只对预算外候选做逐条扫描;减少重复扫描请求,不改变原池的选择证据。"
       "仅在 judge.two_stage 开启时生效。", "CE_JEV_SCAN_REUSE_FIT", advanced=True),
    _b("judge.adaptive_overflow", False, "无候选时扩大扫描",
       "两段判定第一轮没有任何可注入候选时,才把扫描池扩大到 overflow_pool;有候选的请求不增加判定调用。"
       "仅在 judge.two_stage 开启时生效。", "CE_JEV_ADAPTIVE_OVERFLOW", advanced=True),
    _i("judge.overflow_pool", 36, "扩大扫描上限",
       "adaptive_overflow 触发时的候选上限;只扫描首轮之外的尾部候选。", "CE_JEV_OVERFLOW_POOL",
       min=1, max=100, advanced=True),
    _i("judge.scan_pool", 12, "扫视池大小", "两段判定第一阶段扫描的候选数(原单批候选与类比候选另加)。",
       "CE_JEV_SCAN_POOL", min=1, advanced=True),
    _i("judge.scan_batch", 3, "每请求候选数",
       "第一阶段每个请求最多装几条候选(超 judge.token_budget 估算还会提前封批)。",
       "CE_JEV_SCAN_BATCH", min=1, advanced=True),
    _i("judge.compare_k", 6, "对比候选数",
       "第二阶段补入扫描概率最高的前几条候选;原单批候选另保留,总量仍受请求预算限制。", "CE_JEV_COMPARE_K",
       min=1, advanced=True),
    _i("judge.concurrency", 8, "判官并发上限",
       "本进程同时发给判官的请求数上限(账号并发上限 10,超出直接 429)。", "CE_TYPESAFE_CONCURRENCY",
       min=1, advanced=True),
    _i("judge.fallback_budget", 0, "判官失灵时注入上限",
       "判官失败或超时时最多注入几条非危急记忆(危急项照常放行)。hardbench 物理实测:判官失灵后"
       "固定注入 5 条,本来稳定会做的题变错率 8.2%(不注入 3.8%),救回率也不比不注入高,所以默认 0。",
       "CE_JEV_FALLBACK_BUDGET", min=0, max=20),
    _b("judge.rescue", True, "救援", "被精排或下限淘汰、但判官强烈认可的候选可以回到注入。", "CE_JEV_RESCUE"),
    _b("judge.credit", True, "适用性学分", "按 prime 时的适用概率给失败反馈加权:高度适用却失败的记忆重罚。",
       "CE_JEV_CREDIT"),
    _e("judge.prompt", "v1", "判定问法",
       (("v1", "v1 · 照做会不会更好"), ("v2", "v2 · 应不应该知道"), ("v3", "v3 · 逐条 v1 + 单选 v2")),
       "改问法等于换了一个判官,选择阈值需重新标定。", "CE_JEV_PROMPT"),
    _b("judge.llm", False, "LLM 第二判官", "与判官并行判定,逐条概率取平均:精度更高、召回略降。", "CE_JUDGE_LLM"),
    _f("judge.llm_timeout", 8.0, "第二判官超时", "", "CE_JUDGE_LLM_TIMEOUT", min=0.5, unit="秒", advanced=True),
    _i("judge.llm_max_tokens", 1500, "第二判官输出上限", "", "CE_JUDGE_LLM_MAX_TOKENS", min=64, advanced=True),
    _b("judge.llm_fallback", True, "大模型接手判定",
       "判官(Jev)失败或超时后,再调用大模型(第二判官模型 model.judge_llm,留空用引擎 LLM)判定注入哪几条。"
       "Jev 正常时不调用;失败时 prime 会多花大模型那几秒(上限 judge.llm_fallback_timeout)。"
       "GLM 请在该模型参数里关闭思考。", "CE_JUDGE_LLM_FALLBACK"),
    _f("judge.llm_fallback_timeout", 5.0, "大模型接手超时",
       "Jev 失败后才调用大模型,最多等这么久。hook 总预算约 9 秒:Jev 快速失败(如上下文超长 422)"
       "时来得及;Jev 超时(judge.timeout)后再接手多半超出 hook 预算,MCP / 评测等不限时的调用仍能受益。",
       "CE_JUDGE_LLM_FALLBACK_TIMEOUT", min=0.5, unit="秒", advanced=True),
    _f("judge.llm_fallback_p", 0.8, "大模型接手 · 适用概率下限",
       "大模型接手时,适用概率 ≥ 此值才注入。以 Jev 的选择为参照标定(物理 56 题,GLM-5.3 关思考):"
       "0.8 时精度 80%、召回 65%。", "CE_JUDGE_LLM_FALLBACK_P", min=0, max=1, advanced=True),
    _i("judge.llm_fallback_max", 2, "大模型接手时注入上限", "", "CE_JUDGE_LLM_FALLBACK_MAX",
       min=1, max=6, advanced=True),
    _f("judge.promote_below", 0.25, "冷启动解锁线",
       "置信度低于此值、但判官高度认可的记忆标为探索项(hook 豁免置信度过滤),让新记忆拿到反馈。",
       "CE_JEV_PROMOTE_BELOW", min=0, max=1, advanced=True),
    _f("judge.promote_p", 0.8, "解锁所需适用概率", "", "CE_JEV_PROMOTE_P", min=0, max=1, advanced=True),
    _i("judge.promote_max_trials", 5, "解锁试用上限", "触发次数达到此值的记忆不再解锁。",
       "CE_JEV_PROMOTE_MAX_TRIALS", min=1, advanced=True),
    _f("judge.explore_p", 0.5, "探索项最低适用概率", "", "CE_JEV_EXPLORE_P", min=0, max=1, advanced=True),

    # ── 选择阈值(线上选择、离线评估老师、导出与判定记录共用)─────────
    _f("selection.choice_min", 0.5, "主选 · 单选概率", "单选概率 ≥ 此值且逐条概率 ≥ 主选逐条下限时入选。",
       "CE_JEV_CHOICE_MIN", min=0, max=1),
    _f("selection.primary_p", 0.3, "主选 · 逐条下限", "", "CE_JEV_PRIMARY_P", min=0, max=1),
    _f("selection.choice_override", 0.6, "主选 · 单选直通",
       "单选很确定时不看逐条概率——知识类记忆(缺陷、判例、论文)按「照做会不会更好」天然拿不到高分。",
       "CE_JEV_CHOICE_OVERRIDE", min=0, max=1),
    _f("selection.supplement_p", 0.85, "补充入选", "同一请求确实需要多条时,逐条概率 ≥ 此值的也注入。",
       "CE_JEV_SUPPLEMENT_P", min=0, max=1),
    _f("selection.none_max", 0.5, "静默门", "单选判「都没用」的概率低于此值,才允许补充、危急与兜底入选。",
       "CE_JEV_NONE_MAX", min=0, max=1),
    _f("selection.rescue_p", 0.9, "救援 · 逐条概率", "被精排淘汰又救回的候选,需逐条概率 ≥ 此值……",
       "CE_JEV_RESCUE_P", min=0, max=1),
    _f("selection.rescue_q", 0.7, "救援 · 单选概率", "……或单选概率 ≥ 此值才可入选。", "CE_JEV_RESCUE_Q",
       min=0, max=1),
    _f("selection.critical_p", 0.7, "危急级入选", "危急级记忆的逐条概率门槛。", "CE_JEV_CRITICAL_P", min=0, max=1),
    _f("selection.analogy_q", 0.7, "类比 · 单选概率", "", "CE_ANALOGY_Q", min=0, max=1),
    _f("selection.analogy_p", 0.6, "类比 · 逐条概率", "", "CE_ANALOGY_P", min=0, max=1),
    _i("selection.hint_max", 2, "方法论提示 · 条数",
       "通用方法论(scope:general)是提示而非事实:不受静默门约束,每次至多注入这么多条。默认 2;设为 0 可关闭。",
       "CE_HINT_MAX", min=0, max=6),
    _f("selection.hint_p", 0.4, "方法论提示 · 逐条概率", "方法论提示入选所需的逐条概率。", "CE_HINT_P",
       min=0, max=1),

    # ── 经验与类比 ─────────────────────────────────────────────────
    _b("exemplar.learn", True, "学习经验样本",
       "记忆在某个真实情境里被证实有用,就把这个情境记为它的经验样本。", "CE_EXEMPLAR_LEARN"),
    _b("exemplar.recall", True, "经验召回", "当前情境也与经验样本比对:见过类似情况就能认出来。",
       "CE_EXEMPLAR_RECALL"),
    _b("exemplar.hint", True, "类比提示", "把最像的过往情境附给精排与判官(「上次在这里适用过」)。",
       "CE_EXEMPLAR_HINT"),
    _e("exemplar.analogy", "on", "类比识别",
       (("on", "开启"), ("shadow", "只问不用(评测对照)"), ("off", "关闭")),
       "问判官「这和那条记忆过去适用的情境是不是同一类情况」,独立于适用性的另一路证据。"
       "三组真实经验流 A/B 均更好(法律判例命中 24.8% → 44.5%)。", "CE_EXEMPLAR_ANALOGY"),
    _i("exemplar.analogy_k", 3, "类比候选数", "过往情境最像的前几条记忆参与类比识别。", "CE_ANALOGY_K",
       min=1, max=10, env_floor=True),
    _i("exemplar.max", 20, "每条记忆样本上限", "超出时淘汰最冗余的一条。", "CE_EXEMPLAR_MAX", min=1, max=200, env_floor=True),
    _b("exemplar.anti", False, "反例经验",
       "失败反馈时记录「不适用的情境」,只提醒判官、不参与召回。真实法律流上无收益,默认关闭。",
       "CE_ANTI_EXEMPLAR"),
    _i("exemplar.anti_max", 10, "反例上限", "", "CE_ANTI_EXEMPLAR_MAX", min=1, max=100, advanced=True, env_floor=True),

    # ── 学习 ───────────────────────────────────────────────────────
    _e("learning.hebbian_rule", "layered", "反馈学习规则",
       (("layered", "分档固定步长"), ("rw", "Rescorla-Wagner 预测误差")), "", "CE_HEBBIAN_RULE"),
    _f("learning.hebbian_alpha", 0.3, "RW 学习率", "", "CE_HEBBIAN_ALPHA", min=0, max=1, advanced=True),
    _b("learning.dedup", True, "语义去重", "巩固时让判官判定近重复记忆并合并(被吸收的记忆快照记入进化日志)。",
       "CE_DEDUP_JEV"),
    _f("learning.dedup_tau", 0.8, "去重阈值", "判官认定「实质相同」的概率 ≥ 此值才合并。", "CE_DEDUP_TAU",
       min=0.5, max=1),
    _b("learning.skip_known", False, "跳过模型已会的经验",
       "入库前让引擎 LLM 闭卷回答这条经验对应的情境;回答已经涵盖这条经验的要点就不入库,"
       "预训练里就有的通用方法不占记忆。覆盖会话提炼、文档导入、harvest 接口(REST/MCP)以及白日梦、"
       "交叉产生的新记忆;只拦新增,撞上已有记忆的照常强化/补充,手动新建不过滤。每条候选多两次 LLM 调用;"
       "调用失败时照常入库。", "CE_SKIP_KNOWN"),
    _i("learning.capture_chars", 200000, "对话读取上限",
       "提炼经验、被动评估与情景卡读取的对话记录上限(字符);超出时保留开头一段和大部分结尾。"
       "长上下文模型(如 GLM 1M)可以放宽,上下文较短的模型要调小。", "CE_CAPTURE_CHARS", min=2000, max=2000000,
       unit="字符"),
    _i("learning.update_max_chars", 1200, "增补后正文上限",
       "UPDATE 增补后正文超过这个字数且比原文更长时,不再往旧记忆里塞,改为把候选新增为一条记忆"
       "(0 = 不限)。实测增补无上限时单条记忆被更新 166 次、长到 4601 字,判官只看得到前 400 字,"
       "却到处被注入。", "CE_UPDATE_MAX_CHARS", min=0, advanced=True),

    # ── 领域 ───────────────────────────────────────────────────────
    _b("domain.auto", True, "自动归类",
       "新记忆入库时由判官归入已有领域;都不合适时由「语言模型」绑定的模型新建一个。关闭则沿用提炼时给的领域名。",
       "CE_DOMAIN_AUTO"),
    _f("domain.assign_q", 0.5, "归入门槛", "判官单选里最适合的领域概率 ≥ 此值且高于「都不合适」才归入,否则新建。",
       "CE_DOMAIN_ASSIGN_Q", min=0, max=1),
    _i("domain.max_count", 40, "领域数上限", "到上限后不再新建,归入判官认为最接近的领域。", "CE_DOMAIN_MAX",
       min=1, max=500),
    _i("domain.merge_below", 2, "小领域归并",
       "成员不超过此数的领域,巩固时逐条重新归类,判官认定归得进别的领域就并过去,并空的领域删除;0 关闭。",
       "CE_DOMAIN_MERGE_BELOW", min=0, max=100),

    # ── 判定记录 ───────────────────────────────────────────────────
    _b("distill.decision_log", True, "判定记录",
       "记录每次 prime 的完整判定输入、决定与事后结果,可原样重放给任何老师;写入在后台,失败不影响 prime。",
       "CE_DECISION_LOG"),
    Spec("distill.archive_dir", "str", "data/distill/archive", "归档目录",
         "按天追加写 .jsonl.gz,训练数据比数据库活得久。留空关闭归档——关闭后库里的记录永不清理。",
         "CE_DECISION_ARCHIVE_DIR"),
    _f("distill.archive_hours", 6.0, "归档间隔", "", "CE_DECISION_ARCHIVE_HOURS", min=0.1, unit="小时", env_floor=True),
    _i("distill.keep_days", 90, "库内保留天数", "只清理已归档且超过此天数的记录。", "CE_DECISION_LOG_DAYS", min=1, env_floor=True),
    Spec("distill.shadow_judges", "models", [], "影子判官",
         "每次判定后在后台原样作答,记为老师标注,不影响决定——候选学生上岗前的影子期。",
         "CE_SHADOW_JUDGES", kinds=("chat", "judge")),
    _f("distill.shadow_rate", 1.0, "影子抽样率", "贵的大模型老师可以只抽一部分。", "CE_SHADOW_RATE", min=0, max=1),

    # ── System 1 网关 ──────────────────────────────────────────────
    Spec("gateway.route", "models", ["jev"], "判官路由",
         "依次询问:前一位对每道题都有把握就返回,否则升级给下一位。「jev」指当前绑定的适用性判官。",
         "CE_SYSTEMONE_ROUTE", kinds=("chat", "judge")),
    _f("gateway.low", 0.15, "是否题 · 把握下限", "概率 ≤ 此值视为有把握的「否」。", "CE_SYSTEMONE_LOW", min=0, max=0.5),
    _f("gateway.high", 0.85, "是否题 · 把握上限", "概率 ≥ 此值视为有把握的「是」。", "CE_SYSTEMONE_HIGH",
       min=0.5, max=1),
    _f("gateway.choice", 0.6, "单选题 · 把握线", "最高选项概率 ≥ 此值视为有把握。", "CE_SYSTEMONE_CHOICE",
       min=0, max=1),

    # ── 系统 ───────────────────────────────────────────────────────
    Spec("system.qdrant_url", "str", "http://localhost:6333", "Qdrant 地址", "", "CE_QDRANT_URL", restart=True),
    Spec("system.qdrant_collection", "str", "enzymes", "Qdrant 集合名",
         "经验样本与词面索引分别存放在 <集合名>_exemplars、<集合名>_lexical。", "CE_QDRANT_COLLECTION",
         restart=True),
    Spec("system.rules_path", "str", "", "规则文件路径", "导出成熟记忆为规则文件时的写入路径;留空不导出。",
         "CE_RULES_PATH"),
    Spec("system.workspace", "str", "", "工作目录", "扫描项目特征的默认目录;留空用服务启动目录。", "CE_WORKSPACE"),
)

SPECS: dict[str, Spec] = {s.key: s for s in SPECS_LIST}

_values: dict[str, Any] = {}             # 后台保存的值(已校验)
_updated: dict[str, datetime] = {}       # 后台值的最后修改时间


# ── 取值 ──────────────────────────────────────────────────────────

def _floor(spec: Spec, value: float) -> float:
    """环境变量只在改造前就截断过的参数上按下限截断(env_floor);min / max 只约束后台输入,
    不改变评测脚本经环境变量设的旋钮。"""
    if spec.env_floor and spec.min is not None and value < spec.min:
        value = type(value)(spec.min)
    return value


def parse_env(spec: Spec, raw: str) -> Any:
    """环境变量字符串 → 类型化取值;非法值抛 ValueError(调用方回退默认值)。"""
    raw = raw.strip()
    if spec.type == "bool":
        low = raw.lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
        raise ValueError(raw)
    if spec.type == "int":
        return int(_floor(spec, int(float(raw))))
    if spec.type == "float":
        return float(_floor(spec, float(raw)))
    if spec.type == "enum":
        low = raw.lower()
        if low not in {c for c, _ in spec.choices}:
            raise ValueError(raw)
        return low
    if spec.type in ("list", "models"):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if spec.type == "json":
        return json.loads(raw)
    return raw


def coerce(spec: Spec, value: Any) -> Any:
    """后台提交的值 → 校验后的类型化取值;不合法抛 SettingError。"""
    t = spec.type
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return parse_env(spec, value)
            raise ValueError
        if t in ("int", "float"):
            if isinstance(value, bool) or value is None or value == "":
                raise ValueError
            num = float(value)
            if t == "int":
                if num != int(num):
                    raise SettingError(spec.key, "需要整数")
                num = int(num)
            if spec.min is not None and num < spec.min:
                raise SettingError(spec.key, f"不能小于 {spec.min:g}")
            if spec.max is not None and num > spec.max:
                raise SettingError(spec.key, f"不能大于 {spec.max:g}")
            return num
        if t == "enum":
            val = str(value).strip().lower()
            if val not in {c for c, _ in spec.choices}:
                raise SettingError(spec.key, "取值不在可选范围内")
            return val
        if t in ("list", "models"):
            if isinstance(value, str):
                return [p.strip() for p in value.split(",") if p.strip()]
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                return [v.strip() for v in value if v.strip()]
            raise ValueError
        if t == "json":
            if isinstance(value, str):
                return json.loads(value) if value.strip() else {}
            json.dumps(value)
            return value
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError
        return value.strip()
    except SettingError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError):
        raise SettingError(spec.key, f"需要 {t} 类型的值") from None


def _env_value(spec: Spec) -> tuple[bool, Any]:
    if not spec.env:
        return False, None
    raw = os.environ.get(spec.env)
    if raw is None or not raw.strip():
        return False, None
    try:
        return True, parse_env(spec, raw)
    except (ValueError, json.JSONDecodeError):
        logger.warning("Ignoring invalid %s=%r, using default", spec.env, raw)
        return False, None


def get(key: str) -> Any:
    """当前生效值:后台值 > 环境变量 > 默认值。"""
    spec = SPECS[key]
    if key in _values:
        return _values[key]
    ok, val = _env_value(spec)
    if ok:
        return val
    default = spec.default
    return list(default) if isinstance(default, list) else default


def source(key: str) -> str:
    """当前生效值的来源:db | env | default。"""
    if key in _values:
        return "db"
    return "env" if _env_value(SPECS[key])[0] else "default"


# ── 载入与保存 ────────────────────────────────────────────────────

def _decode(rows: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for full_key, raw in rows.items():
        key = full_key[len(PREFIX):]
        spec = SPECS.get(key)
        if spec is None:
            continue
        try:
            out[key] = coerce(spec, json.loads(raw))
        except (SettingError, json.JSONDecodeError):
            logger.warning("Ignoring invalid stored setting %s=%r", key, raw)
    return out


async def read(pg, keys: list[str] | None = None) -> dict[str, Any]:
    """只读数据库里的后台值(不动进程缓存)——load_runtime_config 用,评测脚本读生产库时不会污染旋钮。"""
    rows = await pg.get_settings_with_prefix(PREFIX)
    values = _decode({k: v for k, (v, _) in rows.items()})
    return values if keys is None else {k: v for k, v in values.items() if k in keys}


async def load(pg) -> None:
    """把后台值载入进程缓存(服务启动时 / 其他副本改动后)。"""
    rows = await pg.get_settings_with_prefix(PREFIX)
    values = _decode({k: v for k, (v, _) in rows.items()})
    _values.clear()
    _values.update(values)
    _updated.clear()
    _updated.update({k[len(PREFIX):]: ts for k, (_, ts) in rows.items() if k[len(PREFIX):] in values})


def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


async def save(pg, values: dict[str, Any] | None = None, reset: list[str] | None = None) -> list[dict]:
    """校验并保存后台值;reset 里的键删除后台值(回到环境变量 / 默认值)。返回变更列表。

    任一值不合法时整批不写入(抛 SettingError)。"""
    values = values or {}
    reset = [k for k in (reset or []) if k not in values]
    for key in [*values, *reset]:
        if key not in SPECS:
            raise SettingError(key, "未知参数")
    clean = {k: coerce(SPECS[k], v) for k, v in values.items()}

    changes = []
    for key, val in clean.items():
        before = get(key)
        if key in _values and _same(_values[key], val):
            continue
        changes.append({"key": key, "before": before, "after": val, "before_source": source(key)})
    for key in reset:
        if key in _values:
            before = _values[key]
            changes.append({"key": key, "before": before, "after": None, "before_source": "db"})

    if clean or reset:
        await pg.set_settings({PREFIX + k: json.dumps(v, ensure_ascii=False) for k, v in clean.items()},
                              delete=[PREFIX + k for k in reset])
    await load(pg)
    for c in changes:
        if c["after"] is None:
            c["after"] = get(c["key"])
    return changes


# ── 对外展示 ──────────────────────────────────────────────────────

def describe(key: str) -> dict:
    spec = SPECS[key]
    env_ok, env_val = _env_value(spec)
    updated = _updated.get(key)
    return {
        "key": key,
        "group": spec.group,
        "type": spec.type,
        "label": spec.label,
        "help": spec.help,
        "default": spec.default,
        "value": get(key),
        "source": source(key),
        "env": spec.env,
        "env_value": env_val if env_ok else None,
        "choices": [{"value": v, "label": lbl} for v, lbl in spec.choices],
        "min": spec.min,
        "max": spec.max,
        "unit": spec.unit,
        "restart": spec.restart,
        "advanced": spec.advanced,
        "kinds": list(spec.kinds),
        "updated_at": updated.isoformat() if updated else None,
    }


def snapshot() -> dict:
    return {
        "groups": [{"key": g.key, "label": g.label, "description": g.description} for g in GROUPS],
        "settings": [describe(s.key) for s in SPECS_LIST],
    }


@contextmanager
def override(values: dict[str, Any]) -> Iterator[None]:
    """测试用:临时覆盖后台值。"""
    saved = dict(_values)
    try:
        _values.update({k: coerce(SPECS[k], v) for k, v in values.items()})
        yield
    finally:
        _values.clear()
        _values.update(saved)
