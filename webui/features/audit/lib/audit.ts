import {
  BrainCircuitIcon,
  BoxIcon,
  DatabaseIcon,
  DatabaseZapIcon,
  KeyRoundIcon,
  ListChecksIcon,
  PlugIcon,
  ServerCogIcon,
  SlidersHorizontalIcon,
  SproutIcon,
  UserIcon,
  type LucideIcon,
} from "lucide-react"

import type { AuditEntry } from "@/lib/api/types"
import { formatInterval, formatNumber } from "@/lib/format"
import { AUDIT_ACTION, labelOf } from "@/lib/labels"

/**
 * 审计动作按点号前缀分类;后端的 action 过滤正是按这个前缀匹配(`user` 命中 `user.*`)。
 * 顺序即筛选菜单里的顺序:先配置,再账号与安全,再数据与运维。
 */
export const AUDIT_CATEGORIES: { value: string; label: string; icon: LucideIcon }[] = [
  { value: "settings", label: "运行参数", icon: SlidersHorizontalIcon },
  { value: "provider", label: "服务商", icon: PlugIcon },
  { value: "model", label: "模型", icon: BoxIcon },
  { value: "security", label: "访问与安全", icon: KeyRoundIcon },
  { value: "user", label: "用户", icon: UserIcon },
  { value: "enzyme", label: "酶库", icon: DatabaseIcon },
  { value: "consolidation", label: "巩固", icon: BrainCircuitIcon },
  { value: "gardener", label: "园丁", icon: SproutIcon },
  { value: "job", label: "后台任务", icon: ListChecksIcon },
  { value: "system", label: "系统", icon: ServerCogIcon },
  { value: "distill", label: "判定记录", icon: DatabaseZapIcon },
]

/** 改变系统行为的配置类操作,统计卡里单独计数 */
export const CONFIG_CATEGORIES = new Set(["settings", "provider", "model", "security"])

export function categoryOf(action: string) {
  return action.split(".")[0] ?? action
}

export function categoryMeta(category: string) {
  return AUDIT_CATEGORIES.find((item) => item.value === category) ?? { value: category, label: category, icon: BoxIcon }
}

export function actionLabel(action: string) {
  return AUDIT_ACTION[action] ?? action
}

export type ActionTone = "destructive" | "warning" | "success" | "info" | "neutral"

/** 这条操作的性质:删除类标红,安全类标黄,新建标绿,运维触发标紫,其余是普通修改。 */
export function actionTone(action: string): ActionTone {
  if (/\.(delete|bulk_delete|bulk_reject)$/.test(action)) return "destructive"
  if (action.startsWith("security.")) return "warning"
  if (/\.(create|bulk_activate|import_env)$/.test(action)) return "success"
  if (/^(system|job|distill)\./.test(action) || action === "consolidation.run") return "info"
  return "neutral"
}

export const TONE_DOT: Record<ActionTone, string> = {
  destructive: "bg-destructive",
  warning: "bg-amber-500",
  success: "bg-emerald-500",
  info: "bg-sky-500",
  neutral: "bg-muted-foreground/50",
}

export const TONE_LABEL: Record<ActionTone, string> = {
  destructive: "删除",
  warning: "安全",
  success: "新建",
  info: "运维",
  neutral: "修改",
}

// ── 明细字段 ────────────────────────────────────────────

/** 明细里常见字段的中文名;不认识的字段原样显示 */
export const DETAIL_KEY_LABEL: Record<string, string> = {
  role: "角色",
  is_active: "启用",
  password: "密码",
  display_name: "显示名",
  action: "操作",
  enabled: "启用",
  queued: "排队条数",
  then: "建议",
  ids: "条目",
  fields: "改动字段",
  protocol: "协议",
  base_url: "接口地址",
  api_key: "API 密钥",
  headers: "请求头",
  name: "名称",
  note: "备注",
  models: "连带删除的模型",
  unbound: "解除的绑定",
  kind: "类型",
  model: "模型名",
  provider_id: "服务商",
  params: "参数",
  created: "新建",
  bindings: "绑定",
  teachers: "老师模型",
  interval: "巩固间隔",
  dreamglia_enabled: "DreamGlia",
  dreamglia_max_dreams: "每次梦境数",
  mutation_enabled: "突变",
  crossover_enabled: "交叉",
  replay_enabled: "情景回放",
  replay_max_episodes: "每次回放情景数",
  mode: "运行方式",
  model_id: "模型库模型",
  repo_path: "目标仓库",
  api_url: "CE 地址",
  max_enzymes: "每次最多酶数",
  timeout_minutes: "超时(分钟)",
  allowed_tools: "允许的工具",
  docker_image: "Docker 镜像",
  docker_host: "Docker 主机",
  nightly_enabled: "夜间定时",
  nightly_hour: "夜巡时刻",
  decision_log: "判定记录",
  teacher_labels: "老师标注",
  decision_outcomes: "结果回传",
}

const TOKEN_ACTION: Record<string, string> = { rotate: "轮换", set: "设置", clear: "清除" }
const KEY_CHANGE: Record<string, string> = { changed: "已更换", cleared: "已清除" }

export function detailKeyLabel(key: string) {
  return DETAIL_KEY_LABEL[key] ?? key
}

/** 明细里的一个值,按字段语义说成人话;复杂结构交给调用方用 JSON 展示。 */
export function formatDetailValue(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "(空)"
  if (key === "role" && typeof value === "string") return labelOf.role(value).label
  if (key === "kind" && typeof value === "string") return labelOf.kind(value).label
  if (key === "protocol" && typeof value === "string") return labelOf.protocol(value).label
  if (key === "action" && typeof value === "string") return TOKEN_ACTION[value] ?? value
  if (key === "password" && value === "reset") return "已重置"
  if (key === "api_key") {
    if (typeof value === "boolean") return value ? "已设置" : "未设置"
    if (typeof value === "string") return KEY_CHANGE[value] ?? value
  }
  if (key === "interval" && typeof value === "number") return formatInterval(value)
  if (key === "nightly_hour" && typeof value === "number") return `${String(value).padStart(2, "0")}:00`
  if (key === "mode" && typeof value === "string") return value === "docker" ? "docker 一次性容器" : "宿主机 claude CLI"
  if (typeof value === "boolean") {
    return /(^is_active$|enabled$)/.test(key) ? (value ? "开启" : "关闭") : value ? "是" : "否"
  }
  if (typeof value === "number") return formatNumber(value)
  if (typeof value === "string") return value
  if (Array.isArray(value)) {
    if (value.length === 0) return "(空列表)"
    if (value.every((item) => ["string", "number", "boolean"].includes(typeof item))) return value.join("、")
    return `${value.length} 项`
  }
  return JSON.stringify(value)
}

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/** 值是否简单到能在一行里读完(否则明细表里退回 JSON) */
export function isSimpleValue(value: unknown): boolean {
  if (value === null || ["string", "number", "boolean", "undefined"].includes(typeof value)) return true
  return Array.isArray(value) && value.length <= 12 && value.every((item) => ["string", "number", "boolean"].includes(typeof item))
}

export interface SettingChange {
  key: string
  before: unknown
  after: unknown
  before_source?: string
}

/** settings.update / model.bind 的明细是一组 before → after;其余动作没有这个形状。 */
export function changesOf(entry: AuditEntry): SettingChange[] | null {
  const changes = entry.detail?.changes
  if (!Array.isArray(changes)) return null
  return changes.filter((item): item is SettingChange => isPlainObject(item) && typeof item.key === "string")
}

function short(text: string, max = 60) {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/**
 * 网格里"对象"下面那一行:把明细压成一句话。认得的动作说人话,其余列出前几个字段。
 * 只读明细里已有的内容,不猜测后端没记的东西。
 */
export function summarize(entry: AuditEntry): string {
  const detail = entry.detail ?? {}
  const changes = changesOf(entry)
  if (changes) {
    if (changes.length === 1) {
      const [change] = changes
      return short(`${change.key}:${formatDetailValue("", change.before)} → ${formatDetailValue("", change.after)}`, 90)
    }
    return `${changes.length} 项:${short(changes.map((change) => change.key).join("、"), 80)}`
  }
  switch (entry.action) {
    case "security.auth":
      return detail.enabled ? "开启访问认证" : "关闭访问认证"
    case "security.api_token":
    case "security.mcp_token":
      return `${TOKEN_ACTION[String(detail.action)] ?? String(detail.action ?? "")}令牌`
    case "system.reindex":
      return `${formatNumber(Number(detail.queued ?? 0))} 条记忆重新排队写入向量索引`
    case "system.reload":
      return "从数据库重新载入运行参数与模型注册表"
    case "job.retry":
      return "失败任务重新排队"
    case "consolidation.run":
      return "手动排队一次巩固"
    case "user.delete":
      return "账号已删除"
    case "enzyme.create":
    case "enzyme.delete":
      return typeof detail.then === "string" && detail.then ? short(detail.then, 80) : ""
    case "enzyme.update":
      return Array.isArray(detail.fields) ? `改动 ${detail.fields.join("、")}` : ""
    case "model.delete":
      return Array.isArray(detail.unbound) && detail.unbound.length
        ? `同时解除 ${detail.unbound.join("、")} 的绑定`
        : "未被任何角色使用"
    case "provider.delete":
      return Array.isArray(detail.models) && detail.models.length ? `连带删除 ${detail.models.length} 个模型` : "没有挂着的模型"
  }
  if (entry.action.startsWith("enzyme.bulk_") && Array.isArray(detail.ids)) return `${detail.ids.length} 条酶`
  const parts = Object.entries(detail)
    .filter(([, value]) => isSimpleValue(value))
    .slice(0, 4)
    .map(([key, value]) => `${detailKeyLabel(key)} ${formatDetailValue(key, value)}`)
  return short(parts.join(" · "), 90)
}

/** 审计对象指向的页面(能跳转的才给链接)。 */
export function targetHref(entry: AuditEntry): string | null {
  const target = entry.target
  if (!target) return null
  if (entry.action === "user.create" || entry.action === "user.update") return `/admin/users?user=${encodeURIComponent(target)}`
  if (entry.action === "enzyme.create" || entry.action === "enzyme.update") return `/enzymes?id=${encodeURIComponent(target)}`
  if (entry.action.startsWith("provider.") && entry.action !== "provider.delete") return "/models/providers"
  if (entry.action.startsWith("model.") && entry.action !== "model.delete") {
    return entry.action === "model.bind" ? "/models/bindings" : "/models/providers"
  }
  if (entry.action === "settings.update") return "/settings/runtime"
  if (entry.action === "job.retry") return "/admin/jobs"
  return null
}
