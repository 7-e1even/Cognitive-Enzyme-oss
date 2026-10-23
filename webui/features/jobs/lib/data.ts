import type { Job } from "@/lib/api/types"

// ── 状态页签 ────────────────────────────────────────────────

export const STATUS_TABS = [
  { value: "all", label: "全部" },
  { value: "pending", label: "排队中" },
  { value: "running", label: "运行中" },
  { value: "succeeded", label: "成功" },
  { value: "failed", label: "失败" },
] as const

export type StatusTab = (typeof STATUS_TABS)[number]["value"]

export function parseStatus(raw: string | null): StatusTab {
  return STATUS_TABS.some((tab) => tab.value === raw) ? (raw as StatusTab) : "all"
}

/** 状态点的颜色(列表、页签、详情头部共用)。 */
export const STATUS_DOT: Record<string, string> = {
  pending: "bg-muted-foreground/60",
  running: "bg-sky-500",
  succeeded: "bg-emerald-500",
  failed: "bg-destructive",
}

// ── 任务类型与触发来源 ──────────────────────────────────────

export const KNOWN_KINDS = ["capture", "consolidate"] as const

export const KIND_HINT: Record<string, string> = {
  capture: "会话结束时 hook 提交的对话记录:提炼新记忆、强化或作废旧记忆,并对本会话推送过的记忆做被动评估。",
  consolidate: "定期或手动触发的巩固:衰减、合并相似与语义重复、免疫巡检、情景回放,最后重写规则文件。",
}

const TRIGGER_LABEL: Record<string, string> = {
  scheduled: "定时",
  manual: "手动",
}

export function triggerLabel(trigger: string | null | undefined) {
  if (!trigger) return "—"
  return TRIGGER_LABEL[trigger] ?? trigger
}

// ── 结果字段 ────────────────────────────────────────────────

/** 后台 worker 写回的 result 字段 → 中文(见后端 app/learning/capture.py 与 consolidation.py)。 */
export const RESULT_LABEL: Record<string, string> = {
  // 会话捕获
  extracted: "提炼候选",
  added: "新增记忆",
  updated: "更新记忆",
  reinforced: "强化记忆",
  invalidated: "作废记忆",
  skipped: "跳过",
  known: "跳过 · 模型已会",
  passive_helped: "被动评估 · 有帮助",
  passive_neutral: "被动评估 · 无影响",
  passive_contradicted: "被动评估 · 矛盾",
  episode: "情景记忆",
  // 记忆巩固
  total_enzymes: "巩固后记忆总数",
  decayed: "置信度衰减",
  dormant_count: "转为休眠",
  pruned_mutants: "修剪失败突变体",
  merged_similar: "合并相似记忆",
  merged_semantic: "合并语义重复",
  mutations_created: "新增突变",
  mutations_pruned: "修剪突变",
  crossovers_created: "新增交叉",
  speculative_pruned: "隔离超额试探记忆",
  zombies: "隔离僵尸记忆",
  stale: "隔离过期记忆",
  contradictions: "发现矛盾",
  rules_enzymes: "写入规则文件",
  replay: "情景回放",
  errors: "出错",
}

// ── 时间 ────────────────────────────────────────────────────

function ms(iso: string | null | undefined) {
  if (!iso) return null
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? null : t
}

/**
 * 从入队到结束用了多久(秒)。后端只记入队、最后更新与结束时间,没有"开始执行"时间,
 * 所以这里是含排队的总耗时;还在跑的任务按当前时间算。
 */
export function jobSeconds(job: Job, now = Date.now()) {
  const start = ms(job.created_at)
  if (start === null) return null
  const end = job.status === "pending" || job.status === "running" ? now : ms(job.finished_at) ?? ms(job.updated_at)
  if (end === null) return null
  return Math.max(0, (end - start) / 1000)
}

/** 短一点的耗时:"850 毫秒" / "5.3 秒" / "2 分 04 秒" / "1 小时 12 分"。 */
export function shortDuration(seconds: number | null) {
  if (seconds === null) return "—"
  if (seconds < 1) return `${Math.round(seconds * 1000)} 毫秒`
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`
  const s = Math.round(seconds)
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, "0")} 秒`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`
  return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`
}

// ── 重试 ────────────────────────────────────────────────────

/**
 * 能不能重试。后端只接受"失败且仍保留输入"的任务:会话捕获跑完(无论成败)都会清掉对话原文,
 * 所以失败的捕获任务几乎总是重试不了——这里提前说明,而不是让人点了再收一个 409。
 */
export function retryState(job: Job): { ok: boolean; reason?: string } {
  if (job.status !== "failed") return { ok: false, reason: "只有失败的任务可以重试" }
  if (job.kind === "capture" && !job.transcript_chars) {
    return {
      ok: false,
      reason: "会话捕获结束后会清除对话原文(隐私要求),这条任务已经没有可重放的输入。下次会话结束时 hook 会重新提交。",
    }
  }
  return { ok: true }
}

export const isActive = (status: string) => status === "pending" || status === "running"
