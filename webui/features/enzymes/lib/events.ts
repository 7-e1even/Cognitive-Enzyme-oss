// 进化事件的读法:把各类事件的 detail(形状见 app/learning/*.py 的 log_event)读成一句中文,
// 并从中找出「为什么被隔离」和质量门的评分明细。
import type { EvolutionEvent } from "@/lib/api/types"
import { shortId } from "@/lib/format"

type Detail = Record<string, unknown>

const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null)
const str = (value: unknown) => (typeof value === "string" && value.trim() ? value.trim() : null)
const fixed = (value: unknown, digits = 2) => {
  const n = num(value)
  return n === null ? "—" : n.toFixed(digits)
}

export const ENZYME_TYPE_LABEL: Record<string, string> = {
  reflex: "反射",
  composite: "组合",
  meta: "元规则",
}

const GATE_DECISION: Record<string, string> = {
  pass: "直接通过",
  refine: "精炼后存入",
  quarantine: "隔离待审",
  reject: "拒收",
}

const CONSOLIDATION_ACTION: Record<string, string> = {
  decay: "置信度衰减",
  dormant: "转入休眠",
  merge_similar: "合并了相似记忆",
  merge_semantic_duplicate: "合并了语义重复的记忆",
  capture_update: "会话捕获时更新",
}

const IMMUNE_PATROL: Record<string, string> = {
  speculative_overflow: "推测性记忆超出容量预算",
  zombie: "长期低精度",
  stale: "长期没有被用到",
  contradiction: "与另一条记忆矛盾",
}

const REPLAY_VERDICT: Record<string, string> = {
  helped: "有帮助",
  contradicted: "被推翻",
  neutral: "无影响",
}

export interface QualityAssessment {
  score: number
  decision: string
  dimensions: Record<string, number>
  insight: string | null
  finalInsight: string | null
  at: string | null
}

/** 质量门对这条记忆的评分(沉淀时写在 harvest 事件里)。事件按时间倒序,取最近一次。 */
export function qualityAssessment(events: EvolutionEvent[] | undefined): QualityAssessment | null {
  const event = events?.find((item) => item.event_type === "harvest" && item.detail?.quality)
  if (!event) return null
  const quality = event.detail.quality as Detail
  const dimensions: Record<string, number> = {}
  for (const [key, value] of Object.entries((quality.dimensions as Detail) ?? {})) {
    const n = num(value)
    if (n !== null) dimensions[key] = n
  }
  return {
    score: num(quality.score) ?? 0,
    decision: str(quality.decision) ?? "",
    dimensions,
    insight: str(event.detail.insight),
    finalInsight: str(event.detail.final_insight),
    at: event.created_at,
  }
}

/** 从一条事件读出它把记忆送进隔离的原因;不是隔离事件时返回 null。 */
export function quarantineReason(event: EvolutionEvent): string | null {
  const detail = event.detail ?? {}
  if (event.event_type === "harvest") {
    const quality = detail.quality as Detail | undefined
    if (quality?.decision !== "quarantine") return null
    return `质量门评分 ${fixed(quality.score, 1)} 分,低于自动通过线 3.5`
  }
  if (event.event_type === "immune" && detail.action === "quarantine") {
    switch (detail.patrol_type) {
      case "zombie":
        return `免疫巡检:触发 ${num(detail.trigger_count) ?? "—"} 次后精度只有 ${fixed(detail.precision)}`
      case "stale":
        return `免疫巡检:创建 ${num(detail.age_days) ?? "—"} 天仍没有被用到`
      case "speculative_overflow":
        return "免疫巡检:推测性记忆超出容量预算"
      case "contradiction":
        return `免疫巡检:与另一条记忆矛盾${str(detail.reason) ? `(${str(detail.reason)})` : ""}`
      default:
        return "免疫巡检送回隔离"
    }
  }
  return null
}

/** 最近一次把它送进隔离的事件;没有对应事件多半是有人手动送审。 */
export function latestQuarantineReason(events: EvolutionEvent[] | undefined) {
  for (const event of events ?? []) {
    const reason = quarantineReason(event)
    if (reason) return { reason, at: event.created_at }
  }
  return null
}

/** 事件的一句话摘要;读不出来的类型返回 null,只展示原始 JSON。 */
export function summarizeEvent(event: EvolutionEvent): string | null {
  const d = event.detail ?? {}
  switch (event.event_type) {
    case "hebbian": {
      const before = (d.before as Detail | undefined)?.confidence
      const after = (d.after as Detail | undefined)?.confidence
      const parts = [
        d.success ? "成功反馈" : "误报反馈",
        `置信度 ${fixed(before)} → ${fixed(after)}`,
      ]
      if (d.learned_exemplar) parts.push("记下一条经验")
      if (str(d.learned_negation)) parts.push(`学到排除条件「${str(d.learned_negation)}」`)
      return parts.join(" · ")
    }
    case "harvest": {
      const quality = d.quality as Detail | undefined
      if (!quality) return "从会话经验中沉淀"
      return `质量门 ${fixed(quality.score, 1)} 分 · ${GATE_DECISION[String(quality.decision)] ?? String(quality.decision)}`
    }
    case "exemplar": {
      const kind = d.kind === "anti" ? "记下反例" : "记下经验"
      return str(d.exemplar) ? `${kind}:${str(d.exemplar)}` : kind
    }
    case "consolidation": {
      const action = CONSOLIDATION_ACTION[String(d.action)] ?? "巩固"
      if (d.action === "decay") return `${action} ${fixed(d.before)} → ${fixed(d.after)}`
      if (str(d.absorbed_id)) return `${action}(${shortId(str(d.absorbed_id))})`
      return str(d.reason) ? `${action}:${str(d.reason)}` : action
    }
    case "mutation":
      if (d.action === "prune_failed_mutant") return `突变体精度 ${fixed(d.precision)},被修剪`
      return `由 ${shortId(str(d.parent_id))} 突变而来${str(d.strategy) ? `(${str(d.strategy)})` : ""}`
    case "crossover":
      return `由 ${shortId(str(d.parent_a))} × ${shortId(str(d.parent_b))} 交叉而来`
    case "dream":
      return str(d.insight) ? `梦境演化:${str(d.insight)}` : "梦境演化"
    case "genesis":
      return str(d.insight) ? `从 ${shortId(str(d.parent))} 分化:${str(d.insight)}` : "分化新生"
    case "immune": {
      const patrol = IMMUNE_PATROL[String(d.patrol_type)] ?? "免疫巡检"
      if (d.action === "degraded") return `${patrol},置信度 ${fixed(d.confidence_before)} → ${fixed(d.confidence_after)}`
      return d.action === "quarantine" ? `${patrol},送回隔离` : patrol
    }
    case "replay": {
      const verdict = REPLAY_VERDICT[String(d.verdict)] ?? String(d.verdict ?? "")
      return str(d.evidence) ? `情景回放判定${verdict}:${str(d.evidence)}` : `情景回放判定${verdict}`
    }
    default:
      return null
  }
}
