import type { ChartConfig } from "@/components/ui/chart"
import type { ActivityPoint, Capabilities, EvolutionEvent } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"

// ── 近期活动曲线 ────────────────────────────────────────────

/** 时间范围(天)。放在 URL 的 `?days=` 里,刷新和分享都保留。 */
export const RANGE_OPTIONS = [
  { value: "7", label: "7 天" },
  { value: "14", label: "14 天" },
  { value: "30", label: "30 天" },
] as const

export type RangeValue = (typeof RANGE_OPTIONS)[number]["value"]

export function parseRange(raw: string | null): RangeValue {
  return RANGE_OPTIONS.some((option) => option.value === raw) ? (raw as RangeValue) : "14"
}

/**
 * 四条曲线,各用一个固定色(明暗两种主题下都看得清)。
 *
 * 这四个量彼此独立——一次 prime、一条反馈、一次会话捕获、一个进化事件不是同一件事的
 * 几个部分——叠起来的"总数"没有意义,所以画成四条不叠加的线。
 */
export const ACTIVITY_SERIES = [
  {
    key: "decisions",
    label: "prime 判定",
    hint: "智能体每次提问前调用 prime 召回记忆,记一条判定。",
  },
  {
    key: "outcomes",
    label: "事后反馈",
    hint: "report 的成功 / 失败,以及会话捕获时的被动评估(有帮助、矛盾、本该想起)。",
  },
  {
    key: "captures",
    label: "会话捕获",
    hint: "会话结束后提交的 capture 任务:从对话里提炼新记忆、强化或作废旧记忆。",
  },
  {
    key: "events",
    label: "进化事件",
    hint: "反馈学习、巩固合并、经验样本、免疫修剪等对记忆库的每一次改动。",
  },
] as const

export type ActivityKey = (typeof ACTIVITY_SERIES)[number]["key"]

export const activityChartConfig = {
  decisions: { label: "prime 判定", color: "var(--color-sky-500)" },
  outcomes: { label: "事后反馈", color: "var(--color-emerald-500)" },
  captures: { label: "会话捕获", color: "var(--color-amber-500)" },
  events: { label: "进化事件", color: "var(--color-violet-500)" },
} satisfies ChartConfig

export function sumSeries(points: ActivityPoint[], key: ActivityKey) {
  return points.reduce((total, point) => total + (point[key] ?? 0), 0)
}

/**
 * 与"前一段同样长的时间"相比的变化(百分比,取整)。
 *
 * 拿本窗口总量对比上一个同长窗口,而不是窗口前后半段——后者只会反映
 * "白天比夜里忙"。前一窗口为 0 时没有可比的基数,返回 null,界面上不画箭头。
 */
export function windowChange(current: number, previous: number): number | null {
  if (previous <= 0) return null
  return Math.round(((current - previous) / previous) * 100)
}

// ── 构成 ────────────────────────────────────────────────────

/** 领域为空的记忆(通用规则)在界面上的名字。 */
export const NO_DOMAIN = "通用"

export function domainLabel(domain: string) {
  return domain.trim() ? domain : NO_DOMAIN
}

/** 按数量降序的 [键, 数量] 列表,数量为 0 的不要。 */
export function sortedEntries(record: Record<string, number> | undefined) {
  return Object.entries(record ?? {})
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1])
}

// ── 模型能力 ────────────────────────────────────────────────

/** 四种能力缺失时各自会让什么停下来(能力来自 /api/dashboard 的 capabilities)。 */
export const CAPABILITIES: { key: keyof Capabilities; label: string; name: string; missing: string }[] = [
  { key: "llm", label: "LLM", name: "对话模型(LLM)", missing: "会话捕获无法提炼新记忆,梦境演化与回放考试停摆" },
  {
    key: "embedding",
    label: "向量",
    name: "向量模型",
    missing: "只能用词面(BM25)召回,语义检索与经验召回不可用",
  },
  { key: "rerank", label: "精排", name: "精排模型", missing: "跳过精排,相关性下限失效,改用模式打分排序" },
  { key: "judge", label: "判官", name: "判官", missing: "不做适用性判定,按固定剂量注入" },
]

// ── 进化事件摘要 ────────────────────────────────────────────

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}

function signed(value: number, digits = 2) {
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`
}

const CONSOLIDATION_ACTION: Record<string, string> = {
  merge_similar: "合并了触发条件高度重合的相似记忆",
  merge_semantic_duplicate: "判官判定语义重复,已合并",
  decay: "长期不用或精度偏低,置信度衰减",
  dormant: "精度过低,转为休眠",
  capture_update: "会话捕获后更新了内容",
}

const IMMUNE_PATROL: Record<string, string> = {
  zombie: "僵尸记忆(反复触发却几乎无效),已隔离",
  stale: "长期未被触发,已隔离待复核",
  speculative_overflow: "试探性记忆超出配额,已隔离",
  contradiction: "与其他记忆矛盾",
}

/**
 * 一句话说清一条进化事件做了什么。
 *
 * detail 的形状因事件类型而异(见后端 app/learning/*),这里只取稳定的几个字段;
 * 认不出的类型退回到 detail 里第一段文字,再不行就不写,只留类型徽章。
 */
export function describeEvent(event: EvolutionEvent): string {
  const d = (event.detail ?? {}) as Record<string, unknown>

  switch (event.event_type) {
    case "hebbian": {
      const delta = num(d.delta)
      const after = num((d.after as Record<string, unknown> | undefined)?.confidence)
      const verdict = d.success === true ? "反馈成功" : d.success === false ? "反馈失败" : "收到反馈"
      const negation = text(d.learned_negation)
      const parts = [verdict]
      if (delta !== null) parts.push(`置信度 ${signed(delta)}${after !== null ? ` → ${after.toFixed(2)}` : ""}`)
      if (negation) parts.push(`学到否定条件「${negation}」`)
      else if (d.learned_exemplar === true) parts.push("记下一条经验样本")
      return parts.join(",")
    }
    case "consolidation": {
      const action = text(d.action)
      return CONSOLIDATION_ACTION[action] ?? (text(d.reason) || "巩固调整")
    }
    case "exemplar": {
      const kind = text(d.kind) === "neg" ? "反例" : "经验样本"
      const sample = text(d.exemplar)
      return sample ? `新${kind}:${sample}` : `新增一条${kind}`
    }
    case "genesis":
      return text(d.insight) ? `由反馈新生:${text(d.insight)}` : "由意外成功的反馈新生"
    case "harvest": {
      const insight = text(d.final_insight) || text(d.insight)
      return insight ? `沉淀经验:${insight}` : "从会话中沉淀出一条经验"
    }
    case "replay": {
      const verdict = text(d.verdict)
      const label = verdict === "helped" ? "有帮助" : verdict === "contradicted" ? "与事实矛盾" : "无影响"
      return `情景回放判定:${label}`
    }
    case "immune":
      return IMMUNE_PATROL[text(d.patrol_type)] ?? "免疫巡检处理"
    case "dedup":
      return "语义去重"
    default: {
      const first = Object.values(d).find((value) => typeof value === "string" && value.trim())
      return typeof first === "string" ? first : ""
    }
  }
}

// ── 数字 ────────────────────────────────────────────────────

/** 0~1 的比例 → "85.7%";null(还没有可算的样本)→ "—"。 */
export function ratio(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—"
  return `${(value * 100).toFixed(value >= 0.995 || value === 0 ? 0 : 1)}%`
}

export function count(value: number | null | undefined, unit = "") {
  return `${formatNumber(value ?? 0)}${unit}`
}
