// 后端枚举值的中文名与展示色调(徽章变体),各页共用。

type BadgeTone =
  | "secondary"
  | "outline"
  | "primary-light"
  | "success-light"
  | "warning-light"
  | "info-light"
  | "destructive-light"

interface Label {
  label: string
  tone: BadgeTone
  hint?: string
}

function pick(map: Record<string, Label>, key: string | null | undefined): Label {
  return (key && map[key]) || { label: key || "—", tone: "outline" }
}

export const SEVERITY: Record<string, Label> = {
  critical: { label: "危急", tone: "destructive-light", hint: "永远放行,不受剂量与不应期约束" },
  warning: { label: "警告", tone: "warning-light" },
  info: { label: "提示", tone: "secondary" },
}

export const STATUS: Record<string, Label> = {
  active: { label: "启用", tone: "success-light" },
  quarantine: { label: "待审核", tone: "warning-light", hint: "质量门判为可疑,不参与召回,等待人工审核" },
  rejected: { label: "已拒绝", tone: "outline" },
}

export const ORIGIN: Record<string, Label> = {
  manual: { label: "手动创建", tone: "secondary" },
  harvested: { label: "经验沉淀", tone: "primary-light" },
  evolved: { label: "梦境演化", tone: "info-light" },
  refined: { label: "精炼", tone: "info-light" },
  mutated: { label: "突变", tone: "warning-light" },
  crossed: { label: "交叉", tone: "warning-light" },
  consolidated: { label: "巩固合并", tone: "secondary" },
}

export const EVENT_TYPE: Record<string, Label> = {
  hebbian: { label: "反馈学习", tone: "primary-light" },
  genesis: { label: "新生", tone: "success-light" },
  harvest: { label: "沉淀", tone: "success-light" },
  mutation: { label: "突变", tone: "warning-light" },
  crossover: { label: "交叉", tone: "warning-light" },
  consolidation: { label: "巩固", tone: "secondary" },
  dream: { label: "梦境", tone: "info-light" },
  immune: { label: "免疫修剪", tone: "destructive-light" },
  replay: { label: "情景回放", tone: "info-light" },
  exemplar: { label: "经验样本", tone: "primary-light" },
  dedup: { label: "语义去重", tone: "secondary" },
}

export const JOB_KIND: Record<string, Label> = {
  capture: { label: "会话捕获", tone: "primary-light" },
  consolidate: { label: "记忆巩固", tone: "info-light" },
}

export const JOB_STATUS: Record<string, Label> = {
  pending: { label: "排队中", tone: "secondary" },
  running: { label: "运行中", tone: "info-light" },
  succeeded: { label: "成功", tone: "success-light" },
  failed: { label: "失败", tone: "destructive-light" },
}

export const PROTOCOL: Record<string, Label> = {
  openai: { label: "OpenAI 兼容", tone: "secondary", hint: "Chat Completions / Embeddings / Rerank" },
  anthropic: { label: "Anthropic", tone: "warning-light", hint: "Messages API" },
  system_one: { label: "System One", tone: "info-light", hint: "TypeSafe 判官协议(Jev、laya-serve)" },
}

export const MODEL_KIND: Record<string, Label> = {
  chat: { label: "对话", tone: "primary-light" },
  embedding: { label: "向量", tone: "success-light" },
  rerank: { label: "精排", tone: "warning-light" },
  judge: { label: "判官", tone: "info-light" },
}

export const REASON: Record<string, Label> = {
  primary: { label: "主选", tone: "success-light" },
  supplement: { label: "补充", tone: "primary-light" },
  critical: { label: "危急", tone: "destructive-light" },
  analogy: { label: "类比", tone: "info-light" },
  explorer: { label: "探索", tone: "warning-light" },
  dosage: { label: "剂量", tone: "secondary" },
}

export const ROLE: Record<string, Label> = {
  admin: { label: "管理员", tone: "primary-light" },
  user: { label: "操作员", tone: "secondary" },
}

export const SOURCE: Record<string, Label> = {
  db: { label: "后台", tone: "primary-light", hint: "在后台保存的值" },
  env: { label: "环境变量", tone: "warning-light", hint: "来自部署环境变量;在后台保存后以后台为准" },
  default: { label: "默认", tone: "outline", hint: "内置默认值" },
  none: { label: "未配置", tone: "destructive-light" },
  inherit: { label: "沿用引擎 LLM", tone: "outline" },
}

export const DECISION_SOURCE: Record<string, Label> = {
  prime: { label: "线上 prime", tone: "primary-light" },
  gateway: { label: "System 1 网关", tone: "info-light" },
  eval: { label: "评测", tone: "secondary" },
  stream: { label: "经验流", tone: "secondary" },
}

export const labelOf = {
  severity: (k?: string | null) => pick(SEVERITY, k),
  status: (k?: string | null) => pick(STATUS, k),
  origin: (k?: string | null) => pick(ORIGIN, k),
  event: (k?: string | null) => pick(EVENT_TYPE, k),
  jobKind: (k?: string | null) => pick(JOB_KIND, k),
  jobStatus: (k?: string | null) => pick(JOB_STATUS, k),
  protocol: (k?: string | null) => pick(PROTOCOL, k),
  kind: (k?: string | null) => pick(MODEL_KIND, k),
  reason: (k?: string | null) => pick(REASON, k),
  role: (k?: string | null) => pick(ROLE, k),
  source: (k?: string | null) => pick(SOURCE, k),
  decisionSource: (k?: string | null) => pick(DECISION_SOURCE, k),
}

/** 审计动作 → 中文 */
export const AUDIT_ACTION: Record<string, string> = {
  "settings.update": "修改运行参数",
  "provider.create": "新建服务商",
  "provider.update": "修改服务商",
  "provider.delete": "删除服务商",
  "model.create": "新建模型",
  "model.update": "修改模型",
  "model.delete": "删除模型",
  "model.bind": "绑定模型角色",
  "model.import_env": "从环境变量导入模型",
  "user.create": "新建用户",
  "user.update": "修改用户",
  "user.delete": "删除用户",
  "security.auth": "切换访问认证",
  "security.api_token": "变更 API 令牌",
  "security.mcp_token": "变更 MCP 令牌",
  "enzyme.create": "新建酶",
  "enzyme.update": "编辑酶",
  "enzyme.delete": "删除酶",
  "enzyme.bulk_delete": "批量删除酶",
  "enzyme.bulk_activate": "批量启用酶",
  "enzyme.bulk_quarantine": "批量隔离酶",
  "enzyme.bulk_reject": "批量拒绝酶",
  "consolidation.schedule": "修改巩固调度",
  "consolidation.run": "手动触发巩固",
  "gardener.config": "修改园丁配置",
  "job.retry": "重试后台任务",
  "system.reindex": "重建向量索引",
  "system.reload": "重新载入配置",
  "distill.archive": "手动归档判定数据",
}
