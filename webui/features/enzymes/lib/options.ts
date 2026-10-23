// 酶库与隔离审核共用的枚举选项、排序键与本地标签(共享 lib/labels.ts 没有的部分)。
import type { EnzymeStatus, Origin, Severity } from "@/lib/api/types"
import { ORIGIN, SEVERITY, STATUS } from "@/lib/labels"

export const SEVERITY_VALUES = ["critical", "warning", "info"] as const satisfies readonly Severity[]
export const STATUS_VALUES = ["active", "quarantine", "rejected"] as const satisfies readonly EnzymeStatus[]
export const ORIGIN_VALUES = [
  "manual",
  "harvested",
  "refined",
  "evolved",
  "mutated",
  "crossed",
  "consolidated",
] as const satisfies readonly Origin[]

export const SEVERITY_OPTIONS = SEVERITY_VALUES.map((value) => ({ value, label: SEVERITY[value].label }))
export const STATUS_OPTIONS = STATUS_VALUES.map((value) => ({ value, label: STATUS[value].label }))
export const ORIGIN_OPTIONS = ORIGIN_VALUES.map((value) => ({ value, label: ORIGIN[value].label }))

/** 后端 /api/enzymes 接受的排序键(pg_store._ENZYME_SORTS)。 */
export const SORT_KEYS = [
  "updated_at",
  "created_at",
  "confidence",
  "precision",
  "triggers",
  "activations",
  "quality",
  "last_activated",
] as const
export type SortKey = (typeof SORT_KEYS)[number]

export const SORT_LABEL: Record<SortKey, string> = {
  updated_at: "最近更新",
  created_at: "创建时间",
  confidence: "置信度",
  precision: "精度",
  triggers: "触发次数",
  activations: "激活次数",
  quality: "质量分",
  last_activated: "最近使用",
}

/** 置信度下限筛选的档位。 */
export const MIN_CONFIDENCE_STEPS = [0.3, 0.5, 0.7, 0.9] as const

export const PAGE_SIZES = [10, 20, 50, 100]

/** 关联类型(RelationType)。详情接口按两跳查询时不带关系名,所以要有兜底。 */
const RELATION_LABEL: Record<string, string> = {
  co_triggered: "共同触发",
  prerequisite: "前置",
  generalization: "泛化合并",
  evolved_from: "演化自",
  similar: "相似",
}

export function relationLabel(relation: string | null | undefined) {
  return (relation && RELATION_LABEL[relation]) || "关联"
}

/** 质量门的五个评分维度(app/learning/quality_gate.py)。 */
export const QUALITY_DIMENSIONS = [
  { key: "specificity", label: "具体性", hint: "是否足够具体,而不是「注意安全」这类空话" },
  { key: "novelty", label: "新颖性", hint: "与已有记忆是否重复" },
  { key: "accuracy", label: "准确性", hint: "事实是否正确可靠" },
  { key: "universality", label: "通用性", hint: "能否跨项目、跨场景复用" },
  { key: "actionability", label: "可执行性", hint: "建议是否给出了可以照做的步骤" },
] as const

/** 质量门阈值,与后端 quality_gate.py 保持一致。 */
export const QUALITY_GATE = { pass: 3.5, refine: 2.5, quarantine: 2.0 } as const

/** 批量操作审批后的置信度下限(routers/enzymes.py bulk_action)。 */
export const ACTIVATE_CONFIDENCE_FLOOR = 0.4
