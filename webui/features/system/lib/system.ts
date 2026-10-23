import type { Capabilities, SystemStatus } from "@/lib/api/types"

/** 依赖检查项:名字、它在系统里干什么 */
export const DEPENDENCIES: { key: keyof SystemStatus["checks"]; label: string; role: string }[] = [
  { key: "postgresql", label: "PostgreSQL", role: "酶、审计、任务与判定记录的主库" },
  { key: "qdrant", label: "Qdrant", role: "向量索引,语义召回靠它" },
  { key: "mcp", label: "MCP 服务", role: "端口 8421,给编程智能体调用的工具端点" },
  { key: "workers", label: "后台任务", role: "巩固、会话捕获与向量同步的执行器" },
]

export const CAPABILITIES: { key: keyof Capabilities; label: string; hint: string }[] = [
  { key: "llm", label: "对话模型", hint: "质量门、会话捕获、梦境演化与情景回放要用;没有时这些步骤会跳过" },
  { key: "embedding", label: "向量模型", hint: "把记忆写进向量索引,供语义召回" },
  { key: "rerank", label: "精排模型", hint: "对召回候选重新打分;没有时按向量相似度排序" },
  { key: "judge", label: "判官(Jev)", hint: "决定注入哪几条记忆的概率模型;没有时退回启发式规则" },
]

/** 数据表的中文说明;没列出的表只显示表名 */
export const TABLE_LABELS: Record<string, string> = {
  enzymes: "酶(记忆)",
  enzyme_relations: "酶之间的关系",
  evolution_log: "进化事件",
  users: "用户",
  sessions: "会话",
  audit_log: "审计日志",
  background_jobs: "后台任务",
  episodes: "情景片段",
  vector_sync_queue: "向量同步队列",
  model_entries: "模型",
  model_providers: "服务商",
  app_settings: "运行参数与内部状态",
  prime_contexts: "prime 情境",
  prime_injections: "注入记录",
  decision_log: "判定记录",
  decision_outcomes: "判定的真实结果",
  teacher_labels: "老师标注",
  replay_evals: "回放判定",
  capture_evals: "捕获评估",
  schema_migrations: "结构迁移",
}

export interface TableRow {
  name: string
  label: string
  rows: number
  bytes: number
  share: number
}

export function tableRows(tables: SystemStatus["tables"]): TableRow[] {
  const entries = Object.entries(tables ?? {})
  const totalBytes = entries.reduce((sum, [, table]) => sum + (table.bytes || 0), 0)
  return entries.map(([name, table]) => ({
    name,
    label: TABLE_LABELS[name] ?? "",
    rows: table.rows,
    bytes: table.bytes,
    share: totalBytes ? table.bytes / totalBytes : 0,
  }))
}
