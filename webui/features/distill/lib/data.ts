// 判定记录页的中文标签与判定记录的解析(字段形状见后端 app/distill/records.py 与 app/typesafe_client.py)。

type Tone =
  | "secondary"
  | "outline"
  | "primary-light"
  | "success-light"
  | "warning-light"
  | "info-light"
  | "destructive-light"

interface Label {
  label: string
  tone: Tone
  hint?: string
}

function pick(map: Record<string, Label>, key: string | null | undefined): Label {
  return (key && map[key]) || { label: key || "—", tone: "outline" }
}

// ── 事后结果 ────────────────────────────────────────────────

/** 正向 / 负向 / 闭世界(标签完整)三类,与导出脚本 app/distill/export.py 的 POSITIVE / NEGATIVE / CLOSED 一致。 */
const OUTCOME: Record<string, Label> = {
  success: { label: "反馈成功", tone: "success-light", hint: "智能体调用 report,说这条记忆帮上了忙" },
  failure: { label: "反馈失败", tone: "destructive-light", hint: "智能体调用 report,说这条记忆是误报" },
  helped: { label: "有帮助", tone: "success-light", hint: "会话捕获时的被动评估:这条记忆在会话里确实用上了" },
  contradicted: { label: "矛盾", tone: "destructive-light", hint: "会话捕获时的被动评估:这条记忆与会话里的事实相反" },
  revealed: { label: "本该想起", tone: "info-light", hint: "会话捕获发现:这条记忆当时该推送却没推" },
  gold: { label: "标准答案", tone: "success-light", hint: "评测集 / 经验流给出的正确答案" },
  acceptable: { label: "可接受", tone: "success-light", hint: "评测集里可以接受的答案" },
  silent: { label: "应当静默", tone: "secondary", hint: "评测集判定:这一次一条都不该推" },
}

export const outcomeLabel = (key: string | null | undefined) => pick(OUTCOME, key)

export const POSITIVE_OUTCOMES = new Set(["gold", "acceptable", "helped", "success", "revealed"])
export const NEGATIVE_OUTCOMES = new Set(["contradicted", "failure"])

// ── 判定策略 ────────────────────────────────────────────────

const JUDGE: Record<string, Label> = {
  jev: { label: "Jev 判官", tone: "info-light", hint: "判官给出概率,按选择阈值决定注入哪几条" },
  dosage: { label: "固定剂量", tone: "secondary", hint: "判官未作答(未配置、超时或失败),按打分与固定剂量注入" },
}

export const judgeLabel = (key: string | null | undefined) => pick(JUDGE, key)

export const JUDGE_STATUS: Record<string, string> = {
  ok: "正常作答",
  timeout: "超时",
  failed: "调用失败",
  off: "未启用",
}

export const PROMPT_LABEL: Record<string, string> = {
  v1: "v1 · 照做会不会更好",
  v2: "v2 · 应不应该知道",
  v3: "v3 · 逐条 v1 + 单选 v2",
}

export const STAGE_LABEL: Record<string, string> = {
  post_rerank: "精排之后",
  parallel: "与精排并行",
}

export const ANALOGY_LABEL: Record<string, string> = {
  on: "开启",
  shadow: "只问不用(评测对照)",
  off: "关闭",
}

/** 选择阈值(运行参数 selection.*)。顺序即界面上的顺序。 */
export const THRESHOLD_LABEL: [string, string][] = [
  ["choice_min", "主选 · 单选概率"],
  ["primary_p", "主选 · 逐条下限"],
  ["choice_override", "主选 · 单选直通"],
  ["supplement_p", "补充入选"],
  ["none_max", "静默门"],
  ["rescue_p", "救援 · 逐条概率"],
  ["rescue_q", "救援 · 单选概率"],
  ["critical_p", "危急级入选"],
  ["analogy_q", "类比 · 单选概率"],
  ["analogy_p", "类比 · 逐条概率"],
]

export const TIER_LABEL: Record<string, string> = {
  critical: "危急",
  relevant: "相关",
  peripheral: "边缘",
}

/** 统计里的判官分布按"模型名,没有模型时用判官名"分组;判官没作答的记录两者都是空串。 */
export function judgeBucketLabel(key: string) {
  if (!key) return "固定剂量(判官未作答)"
  if (key === "jev") return "Jev 判官"
  if (key === "dosage") return "固定剂量"
  return key
}

// ── 判官题目与作答 ──────────────────────────────────────────

export type RuleText = Record<string, string>

export interface NoulQuestion {
  key: string
  kind: "noul"
  /** 候选规则(c*)或过往情境(a*) */
  rule?: RuleText
  past?: string
  question: string
  truthy?: string
  falsy?: string
}

export interface ChoiceQuestion {
  key: string
  kind: "choice"
  question: string
  options: { key: string; text: string }[]
}

export type Question = NoulQuestion | ChoiceQuestion

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function asText(value: unknown): string {
  if (typeof value === "string") return value
  if (value === null || value === undefined) return ""
  if (typeof value === "object") {
    return Object.entries(asRecord(value))
      .filter(([, v]) => v !== "" && v !== null && v !== undefined)
      .map(([k, v]) => `${k}:${typeof v === "string" ? v : JSON.stringify(v)}`)
      .join("\n")
  }
  return String(value)
}

function ruleOf(value: unknown): RuleText | undefined {
  const record = asRecord(value)
  const entries = Object.entries(record).filter(([, v]) => typeof v === "string" && v.trim())
  return entries.length ? Object.fromEntries(entries as [string, string][]) : undefined
}

/** 题号排序:c0…cN、best、a0…aK、same,其余按字母。 */
function questionOrder(key: string) {
  const m = /^([a-z]+)(\d+)$/.exec(key)
  if (m?.[1] === "c") return [0, Number(m[2])]
  if (key === "best") return [1, 0]
  if (m?.[1] === "a") return [2, Number(m[2])]
  if (key === "same") return [3, 0]
  return [4, 0]
}

/** 把请求里的 questions 整理成有序的题目列表;形状不认识的题目按原文展示,不丢。 */
export function parseQuestions(raw: Record<string, unknown> | undefined | null): Question[] {
  const entries = Object.entries(raw ?? {})
  entries.sort(([a], [b]) => {
    const [ga, ia] = questionOrder(a)
    const [gb, ib] = questionOrder(b)
    return ga - gb || ia - ib || a.localeCompare(b)
  })

  return entries.map(([key, value]) => {
    const q = asRecord(value)
    if (q.type === "choice") {
      const criteria = asRecord(q.criteria)
      return {
        key,
        kind: "choice",
        question: asText(q.instructions),
        options: Object.entries(criteria).map(([optionKey, text]) => ({ key: optionKey, text: asText(text) })),
      } satisfies ChoiceQuestion
    }
    const instructions = asRecord(q.instructions)
    const criteria = asRecord(q.criteria)
    return {
      key,
      kind: "noul",
      rule: ruleOf(instructions.rule),
      past: typeof instructions.past === "string" ? instructions.past : undefined,
      question: typeof q.instructions === "string" ? q.instructions : asText(instructions.question),
      truthy: asText(criteria.true) || undefined,
      falsy: asText(criteria.false) || undefined,
    } satisfies NoulQuestion
  })
}

/** 逐条题的概率(System One answers 形状:{c0: {noul: 0.82}})。 */
export function noulAnswer(answers: unknown, key: string): number | null {
  const value = asRecord(asRecord(answers)[key]).noul
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

/** 单选题各选项的概率({best: {probabilities: {c0: .., none: ..}}})。 */
export function choiceAnswer(answers: unknown, key: string): Record<string, number> | null {
  const probs = asRecord(asRecord(asRecord(answers)[key]).probabilities)
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(probs)) {
    if (typeof v === "number" && Number.isFinite(v)) out[k] = v
  }
  return Object.keys(out).length ? out : null
}

/** 规则字典的第一行摘要(触发 / 适用情境)与正文(建议 / 内容)。 */
export function ruleSummary(rule: RuleText | undefined) {
  if (!rule) return { head: "", body: "" }
  const values = Object.values(rule)
  return { head: values[0] ?? "", body: values[1] ?? "" }
}

export function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

export function str(value: unknown): string {
  return typeof value === "string" ? value : value === null || value === undefined ? "" : String(value)
}
