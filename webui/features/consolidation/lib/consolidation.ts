import type { Schedule } from "@/lib/api/types"

/** 巩固结果里认得的数字:中文名 + 一句解释。键名与后端 full_consolidation 的返回一一对应。 */
export const RESULT_LABELS: Record<string, { label: string; hint?: string }> = {
  decayed: { label: "置信度衰减", hint: "长期没被用上、置信度被调低的记忆" },
  dormant_count: { label: "标为休眠", hint: "触发很多次仍几乎不准,标记休眠(不删除)" },
  pruned_mutants: { label: "清理突变体", hint: "试用期里表现太差、被删掉的突变体" },
  merged_similar: { label: "相似合并", hint: "触发条件和建议几乎一样,合并成一条" },
  merged_semantic: { label: "语义去重", hint: "判官认定意思重复,合并成一条" },
  mutations_created: { label: "新突变体" },
  mutations_pruned: { label: "淘汰突变体" },
  crossovers_created: { label: "新交叉体" },
  dreams_created: { label: "梦境假设", hint: "生成的新假设,进入探索槽等待验证" },
  dreams_tried: { label: "尝试组合", hint: "本次挑出来做梦境的记忆对数" },
  zombies: { label: "僵尸隔离", hint: "触发很多次、几乎从不奏效的记忆" },
  stale: { label: "过期隔离", hint: "很久没有任何反馈的记忆" },
  contradictions: { label: "矛盾处理", hint: "与其他记忆互相矛盾,较弱的一方被降级或隔离" },
  speculative_pruned: { label: "探索超额", hint: "探索中的假设太多,把最不活跃的移入隔离区" },
  rules_enzymes: { label: "写入 Rules", hint: "写进 Rules 文件的成熟记忆条数" },
  total_enzymes: { label: "酶库总数", hint: "巩固结束时酶库里的记忆条数" },
}

/** 情景回放阶段的结果是一个对象 */
export const REPLAY_LABELS: Record<string, { label: string; hint?: string }> = {
  episodes: { label: "回放情景" },
  helped: { label: "确有帮助", hint: "注入的记忆确实帮上了忙,置信度上调" },
  contradicted: { label: "被反驳", hint: "情景证明这条记忆不适用,置信度下调" },
  neutral: { label: "无关" },
  self_excluded: { label: "排除自身", hint: "由这一幕情景本身产生的记忆不参与考试" },
  skipped_empty: { label: "空情景跳过" },
  errors: { label: "出错" },
}

export type StageId = "maintenance" | "evolution" | "dreamglia" | "replay" | "rules"

export interface StageDef {
  id: StageId
  title: string
  description: string
  /** 这一阶段在上次结果里对应的键 */
  resultKeys: string[]
  badges?: string[]
}

/** 巩固流水线,顺序与后端执行顺序一致 */
export const STAGES: StageDef[] = [
  {
    id: "maintenance",
    title: "衰减与合并",
    description:
      "长期没用上的记忆慢慢降低置信度;触发很多次仍几乎不准的标为休眠(不删);试用期里表现差的突变体清掉;内容几乎相同的合并成一条,判官可用时再做一轮语义去重。",
    resultKeys: ["decayed", "dormant_count", "pruned_mutants", "merged_similar", "merged_semantic"],
  },
  {
    id: "evolution",
    title: "突变与交叉",
    description:
      "从置信度高、经常触发的成熟记忆出发:突变改写它的触发条件,交叉把经常一起出现的两条融合成新记忆。新记忆先进试用期,表现差会在之后的巩固里被淘汰。",
    resultKeys: ["mutations_created", "mutations_pruned", "crossovers_created"],
    badges: ["实验性", "LLM 驱动"],
  },
  {
    id: "dreamglia",
    title: "DreamGlia 梦境演化 + 免疫巡逻",
    description:
      "梦境:挑两条相关的记忆,让 LLM 设想它们结合的新情境,生成待验证的假设。免疫巡逻:把僵尸、过期、互相矛盾的记忆与超额的探索假设移入隔离区,等人工审核。",
    resultKeys: ["dreams_created", "dreams_tried", "zombies", "stale", "contradictions", "speculative_pruned"],
  },
  {
    id: "replay",
    title: "情景回放测试",
    description:
      "拿历史会话里的情景离线考一遍现有记忆,重点照顾反馈少的记忆与探索中的假设:确实帮上忙的加分,被情景反驳的扣分。",
    resultKeys: ["replay"],
  },
  {
    id: "rules",
    title: "生成 Rules",
    description: "把最成熟的记忆写进 Rules 文件,供不经过召回的场景直接读取;最后清点酶库。",
    resultKeys: ["rules_enzymes", "total_enzymes"],
  },
]

export const KNOWN_RESULT_KEYS = new Set([...STAGES.flatMap((stage) => stage.resultKeys), "errors", "error"])

// ── 调度表单 ──────────────────────────────────────────────

export type IntervalUnit = "minute" | "hour" | "day"

export const INTERVAL_UNITS: { value: IntervalUnit; label: string; seconds: number }[] = [
  { value: "minute", label: "分钟", seconds: 60 },
  { value: "hour", label: "小时", seconds: 3600 },
  { value: "day", label: "天", seconds: 86400 },
]

export interface ScheduleDraft extends Omit<Schedule, "interval"> {
  intervalAmount: number | null
  intervalUnit: IntervalUnit
}

export function toDraft(schedule: Schedule): ScheduleDraft {
  const { interval, ...rest } = schedule
  const unit = interval % 86400 === 0 ? "day" : interval % 3600 === 0 ? "hour" : "minute"
  const seconds = INTERVAL_UNITS.find((item) => item.value === unit)!.seconds
  return { ...rest, intervalAmount: Math.round((interval / seconds) * 100) / 100, intervalUnit: unit }
}

export function draftInterval(draft: ScheduleDraft): number | null {
  if (draft.intervalAmount === null || !Number.isFinite(draft.intervalAmount)) return null
  const seconds = INTERVAL_UNITS.find((item) => item.value === draft.intervalUnit)!.seconds
  return Math.round(draft.intervalAmount * seconds)
}

/** 草稿与已保存的调度比,只留下改动过的字段——PUT 只发这些,审计日志里也只记这些。 */
export function schedulePatch(draft: ScheduleDraft, saved: Schedule): Partial<Schedule> {
  const patch: Partial<Schedule> = {}
  const interval = draftInterval(draft)
  if (interval !== null && interval !== saved.interval) patch.interval = interval
  const keys = [
    "enabled",
    "dreamglia_enabled",
    "dreamglia_max_dreams",
    "mutation_enabled",
    "crossover_enabled",
    "replay_enabled",
    "replay_max_episodes",
  ] as const
  for (const key of keys) {
    if (draft[key] !== saved[key]) (patch as Record<string, unknown>)[key] = draft[key]
  }
  return patch
}

export function validateDraft(draft: ScheduleDraft): string | null {
  const interval = draftInterval(draft)
  if (interval === null) return "请填写巩固间隔"
  if (interval < 60) return "巩固间隔至少 1 分钟"
  if (draft.dreamglia_max_dreams < 1 || draft.dreamglia_max_dreams > 10) return "每次梦境数在 1–10 之间"
  if (draft.replay_max_episodes < 1 || draft.replay_max_episodes > 20) return "每次回放情景数在 1–20 之间"
  return null
}
