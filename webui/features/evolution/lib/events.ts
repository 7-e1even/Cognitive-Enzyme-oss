import {
  ActivityIcon,
  BookmarkPlusIcon,
  CopyMinusIcon,
  DnaIcon,
  GitMergeIcon,
  LayersIcon,
  MoonStarIcon,
  RotateCcwIcon,
  ShieldAlertIcon,
  SparklesIcon,
  WheatIcon,
  type LucideIcon,
} from "lucide-react"

import type { EvolutionEvent } from "@/lib/api/types"
import { formatProb } from "@/lib/format"

export const EVENT_ICONS: Record<string, LucideIcon> = {
  hebbian: ActivityIcon,
  genesis: SparklesIcon,
  harvest: WheatIcon,
  mutation: DnaIcon,
  crossover: GitMergeIcon,
  consolidation: LayersIcon,
  dream: MoonStarIcon,
  immune: ShieldAlertIcon,
  replay: RotateCcwIcon,
  exemplar: BookmarkPlusIcon,
  dedup: CopyMinusIcon,
}

/** 筛选菜单的顺序:最常见的反馈学习在前,其余按记忆的一生排 */
export const EVENT_TYPE_ORDER = [
  "hebbian",
  "harvest",
  "genesis",
  "exemplar",
  "consolidation",
  "dream",
  "immune",
  "replay",
  "mutation",
  "crossover",
  "dedup",
]

export type Tone = "positive" | "negative" | "neutral"

export interface EventSummary {
  headline: string
  tone: Tone
  /** 置信度 / 精确率这类前后对照 */
  changes: { label: string; from: number; to: number }[]
  /** 明细里的原话:反馈说明、洞见、证据 */
  quote?: string
  /** 明细里指向的其他酶(父代、被吸收的、冲突的) */
  related: { label: string; id: string }[]
  tags: string[]
}

const QUALITY_DECISION: Record<string, string> = {
  pass: "质量门通过",
  refine: "精炼后通过",
  quarantine: "隔离待审",
  reject: "质量门拒绝",
}
const MUTATION_STRATEGY: Record<string, string> = { broaden: "放宽条件", specialize: "收窄条件", rephrase: "改写触发词" }
const CROSSOVER_MODE: Record<string, string> = { conditions_merge: "条件合并", action_merge: "建议融合" }
const REPLAY_VERDICT: Record<string, { label: string; tone: Tone }> = {
  helped: { label: "情景回放:确有帮助", tone: "positive" },
  contradicted: { label: "情景回放:被情景反驳", tone: "negative" },
  neutral: { label: "情景回放:与情景无关", tone: "neutral" },
}
const PATROL: Record<string, string> = {
  zombie: "僵尸记忆,移入隔离区",
  stale: "长期没有反馈,移入隔离区",
  contradiction: "与另一条记忆矛盾",
  speculative_overflow: "探索中的假设超额,移入隔离区",
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function id(value: unknown): string | null {
  return typeof value === "string" && value ? value : null
}

function signed(delta: number) {
  return `${delta >= 0 ? "+" : "−"}${formatProb(Math.abs(delta))}`
}

/**
 * 把一条进化事件的明细读成一句话。只用明细里真有的字段:
 * 反馈学习看成功与否、是否在预期内、置信度的前后值;沉淀看质量门的决定;
 * 巩固 / 免疫看 action / patrol_type;认不出的类型退回"字段: 值"的罗列。
 */
export function summarizeEvent(event: EvolutionEvent): EventSummary {
  const d = event.detail ?? {}
  const summary: EventSummary = { headline: "", tone: "neutral", changes: [], related: [], tags: [] }

  switch (event.event_type) {
    case "hebbian": {
      const success = d.success === true
      const expected = d.expected === true
      const kind = success ? (expected ? "维持性学习" : "探索性学习") : expected ? "纠错性学习" : "确认性削弱"
      const delta = num(d.delta)
      summary.headline = `${success ? "成功反馈" : "失败反馈"} · ${kind}${delta !== null ? ` ${signed(delta)}` : ""}`
      summary.tone = success ? "positive" : "negative"
      const before = (d.before ?? {}) as Record<string, unknown>
      const after = (d.after ?? {}) as Record<string, unknown>
      const c0 = num(before.confidence)
      const c1 = num(after.confidence)
      if (c0 !== null && c1 !== null) summary.changes.push({ label: "置信度", from: c0, to: c1 })
      const p0 = num(before.precision)
      const p1 = num(after.precision)
      if (p0 !== null && p1 !== null && Math.abs(p0 - p1) > 1e-6) summary.changes.push({ label: "精确率", from: p0, to: p1 })
      summary.quote = text(d.detail)
      const credit = num(d.credit)
      if (credit !== null && credit < 1) summary.tags.push(`分摊 ${Math.round(credit * 100)}%`)
      const applicability = num(d.applicability)
      if (applicability !== null) summary.tags.push(`适用概率 ${formatProb(applicability)}`)
      if (d.learned_exemplar) summary.tags.push(success ? "记下正例" : "记下反例")
      const negation = text(d.learned_negation)
      if (negation) summary.tags.push(`学到抑制条件:${negation}`)
      break
    }
    case "harvest": {
      const insight = text(d.final_insight) ?? text(d.insight) ?? "新经验"
      summary.headline = `沉淀新记忆:${insight}`
      const quality = (d.quality ?? {}) as Record<string, unknown>
      const decision = text(quality.decision)
      const score = num(quality.score)
      if (decision) {
        summary.tags.push(`${QUALITY_DECISION[decision] ?? decision}${score !== null ? ` · ${score.toFixed(1)} 分` : ""}`)
        summary.tone = decision === "pass" || decision === "refine" ? "positive" : decision === "reject" ? "negative" : "neutral"
      }
      summary.quote = text(d.context)
      break
    }
    case "genesis": {
      summary.headline = `由反馈衍生出新记忆${text(d.insight) ? `:${text(d.insight)}` : ""}`
      summary.tone = "positive"
      const parent = id(d.parent)
      if (parent) summary.related.push({ label: "父代", id: parent })
      summary.quote = text(d.context)
      break
    }
    case "exemplar": {
      const anti = d.kind === "anti"
      summary.headline = anti ? "记下一条反例情境" : "记下一条正例情境"
      summary.tone = anti ? "negative" : "positive"
      summary.quote = text(d.exemplar)
      const total = num(d.exemplars)
      if (total !== null) summary.tags.push(`正例 ${total} 条`)
      const antiTotal = num(d.anti_exemplars)
      if (antiTotal) summary.tags.push(`反例 ${antiTotal} 条`)
      if (text(d.source)) summary.tags.push(`来源 ${text(d.source)}`)
      break
    }
    case "consolidation": {
      const action = text(d.action)
      if (action === "decay") {
        summary.headline = "长期没用上,置信度衰减"
        summary.tone = "negative"
        const b = num(d.before)
        const a = num(d.after)
        if (b !== null && a !== null) summary.changes.push({ label: "置信度", from: b, to: a })
        summary.quote = text(d.reason)
      } else if (action === "dormant") {
        summary.headline = "精确率过低,标为休眠"
        summary.tone = "negative"
        summary.quote = text(d.reason)
      } else if (action === "merge_similar") {
        summary.headline = "合并了一条几乎相同的记忆"
        const overlap = num(d.pattern_overlap)
        const similarity = num(d.then_similarity)
        if (overlap !== null) summary.tags.push(`触发条件重合 ${formatProb(overlap)}`)
        if (similarity !== null) summary.tags.push(`建议相似 ${formatProb(similarity)}`)
        const absorbed = id(d.absorbed_id)
        if (absorbed) summary.related.push({ label: "被吸收", id: absorbed })
      } else if (action === "merge_semantic_duplicate") {
        summary.headline = "语义去重:吸收了一条意思重复的记忆"
        const p = num(d.dup_probability)
        if (p !== null) summary.tags.push(`重复概率 ${formatProb(p)}`)
        const absorbed = id(d.absorbed_id)
        if (absorbed) summary.related.push({ label: "被吸收", id: absorbed })
      } else if (action === "capture_update") {
        summary.headline = "会话捕获更新了这条记忆"
        summary.tone = "positive"
        summary.quote = text(d.reason)
      } else {
        summary.headline = action ? `巩固:${action}` : "巩固"
      }
      break
    }
    case "dream": {
      summary.headline = `梦境假设:${text(d.insight) ?? "新的组合"}`
      summary.quote = text(d.scenario) ?? text(d.reasoning)
      const parents = Array.isArray(d.parents) ? d.parents : []
      parents.forEach((parent, index) => {
        const pid = id(parent)
        if (pid) summary.related.push({ label: `父代 ${index + 1}`, id: pid })
      })
      break
    }
    case "immune": {
      const patrol = text(d.patrol_type) ?? ""
      summary.headline = `免疫巡逻:${PATROL[patrol] ?? patrol}`
      summary.tone = "negative"
      if (patrol === "zombie") {
        const triggers = num(d.trigger_count)
        const precision = num(d.precision)
        if (triggers !== null) summary.tags.push(`触发 ${triggers} 次`)
        if (precision !== null) summary.tags.push(`精确率 ${formatProb(precision)}`)
      } else if (patrol === "stale") {
        const days = num(d.age_days)
        if (days !== null) summary.tags.push(`已 ${days} 天`)
      } else if (patrol === "contradiction") {
        const b = num(d.confidence_before)
        const a = num(d.confidence_after)
        if (b !== null && a !== null) summary.changes.push({ label: "置信度", from: b, to: a })
        const winner = id(d.conflicts_with)
        if (winner) summary.related.push({ label: "冲突的记忆", id: winner })
        summary.quote = text(d.reason)
      }
      if (text(d.action)) summary.tags.push(d.action === "quarantine" ? "已隔离" : `处理:${text(d.action)}`)
      break
    }
    case "replay": {
      const verdict = REPLAY_VERDICT[String(d.verdict)] ?? { label: `情景回放:${String(d.verdict ?? "")}`, tone: "neutral" as Tone }
      summary.headline = verdict.label
      summary.tone = verdict.tone
      summary.quote = text(d.evidence)
      const credit = num(d.credit)
      if (credit !== null && credit < 1) summary.tags.push(`权重 ${Math.round(credit * 100)}%`)
      break
    }
    case "mutation": {
      if (d.action === "prune_failed_mutant") {
        const precision = num(d.precision)
        summary.headline = `试用期表现太差,突变体被淘汰${precision !== null ? `(精确率 ${formatProb(precision)})` : ""}`
        summary.tone = "negative"
      } else {
        const strategy = text(d.strategy)
        summary.headline = `突变出新记忆${strategy ? `:${MUTATION_STRATEGY[strategy] ?? strategy}` : ""}`
        const parent = id(d.parent_id)
        if (parent) summary.related.push({ label: "父代", id: parent })
        const conf = num(d.parent_confidence)
        if (conf !== null) summary.tags.push(`父代置信度 ${formatProb(conf)}`)
      }
      break
    }
    case "crossover": {
      const mode = text(d.mode)
      summary.headline = `交叉出新记忆${mode ? `:${CROSSOVER_MODE[mode] ?? mode}` : ""}`
      const a = id(d.parent_a)
      const b = id(d.parent_b)
      if (a) summary.related.push({ label: "父代 A", id: a })
      if (b) summary.related.push({ label: "父代 B", id: b })
      break
    }
    default: {
      const parts = Object.entries(d)
        .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
        .slice(0, 3)
        .map(([key, value]) => `${key}: ${String(value)}`)
      summary.headline = parts.join(" · ") || event.event_type
    }
  }
  return summary
}

/** 本地日期的键(YYYY-MM-DD),按天分组用 */
export function dayKey(iso: string | null) {
  if (!iso) return "unknown"
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return "unknown"
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

const dayFormat = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" })
const dayFormatWithYear = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "short" })

/** 分组标题:今天 / 昨天 + 日期 */
export function dayTitle(key: string, now = new Date()) {
  if (key === "unknown") return { primary: "时间未知", secondary: "" }
  const [y, m, d] = key.split("-").map(Number)
  const date = new Date(y, m - 1, d)
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const diff = Math.round((today.getTime() - date.getTime()) / 86_400_000)
  const formatted = (y === now.getFullYear() ? dayFormat : dayFormatWithYear).format(date)
  if (diff === 0) return { primary: "今天", secondary: formatted }
  if (diff === 1) return { primary: "昨天", secondary: formatted }
  return { primary: formatted, secondary: "" }
}

const timeFormat = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })

export function clockTime(iso: string | null) {
  if (!iso) return "—"
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? "—" : timeFormat.format(date)
}
