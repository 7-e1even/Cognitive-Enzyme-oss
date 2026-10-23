// 后端接口的响应类型(与 app/routers/*.py 一一对应)。

export type Role = "admin" | "user"

export interface User {
  id: string
  username: string
  display_name: string
  avatar_url: string
  role: Role
}

export interface AdminUser extends User {
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

export interface Health {
  status: string
  service: string
  version?: string
  llm_configured: boolean
  embedding_configured: boolean
}

// ── 酶(记忆)──────────────────────────────────────────

export type Severity = "critical" | "warning" | "info"
export type EnzymeStatus = "active" | "quarantine" | "rejected"
export type Origin =
  | "manual"
  | "evolved"
  | "harvested"
  | "refined"
  | "mutated"
  | "crossed"
  | "consolidated"

export interface EnzymeRow {
  id: string
  type: string
  patterns: string
  then: string
  because: string
  severity: Severity
  confidence: number
  precision: number
  trigger_count: number
  success_count: number
  false_positive_count: number
  activation_count: number
  origin: Origin
  domain: string
  tags: string[]
  status: EnzymeStatus
  quality_score: number
  exemplars: number
  version: number
  last_activated: string | null
  created_at: string | null
  updated_at: string | null
}

export interface EnzymeListParams {
  q?: string
  domain?: string
  status?: string
  origin?: string
  severity?: string
  min_confidence?: number
  sort?: string
  order?: "asc" | "desc"
  limit?: number
  offset?: number
}

export interface EnzymeFacets {
  domain: Record<string, number>
  status: Record<string, number>
  origin: Record<string, number>
  severity: Record<string, number>
  /** 领域表:一句话描述与来源(existing 原有 / created 自动归类时新建) */
  domain_info?: Record<string, { description: string; source: string }>
}

export interface PatternItem {
  type: "keyword" | "regex"
  value: string
}

export interface EnzymeDetail {
  id: string
  type: string
  version: number
  patterns: string
  pattern_list: PatternItem[]
  suggestion: string
  reason: string
  severity: Severity
  references: string[]
  confidence: number
  precision: number
  stats: { triggers: number; successes: number; false_positives: number }
  origin: Origin
  parent_ids: string[]
  domain: string
  tags: string[]
  exemplars: string[]
  anti_exemplars: string[]
  related?: { id: string; relation: string }[]
  status: EnzymeStatus
  quality_score: number
  conditions: string[]
  negations: string[]
  activation_count: number
  last_activated: string | null
  last_triggered: string | null
  created_at: string | null
  updated_at: string | null
}

export interface EnzymeInput {
  when: string
  then: string
  because: string
  domain?: string
  tags?: string[]
  references?: string[]
  severity?: Severity
}

export interface EnzymeUpdate {
  patterns?: PatternItem[]
  then?: string
  because?: string
  domain?: string
  tags?: string[]
  references?: string[]
  conditions?: string[]
  negations?: string[]
  severity?: Severity
  confidence?: number
  status?: EnzymeStatus
}

export type BulkAction = "delete" | "activate" | "quarantine" | "reject"

export interface SearchHit {
  id: string
  then?: string
  because?: string
  patterns_text?: string
  domain?: string
  confidence?: number
  _score?: number
  [key: string]: unknown
}

export interface EvolutionEvent {
  id: number | null
  enzyme_id: string | null
  event_type: string
  detail: Record<string, unknown>
  created_at: string | null
}

export interface Capabilities {
  llm: boolean
  embedding: boolean
  rerank: boolean
  judge: boolean
}

export interface ActivityPoint {
  day: string
  decisions: number
  outcomes: number
  captures: number
  events: number
}

export interface Dashboard {
  totals: {
    enzymes: number
    avg_confidence: number
    triggers: number
    activations: number
    vectors: number
  }
  by_status: Record<string, number>
  by_origin: Record<string, number>
  by_severity: Record<string, number>
  top_domains: Record<string, number>
  metrics: Metrics
  activity: ActivityPoint[]
  jobs: Record<string, Record<string, number>>
  mature: { id: string; patterns: string; then: string; confidence: number; precision: number; triggers: number }[]
  recent_events: EvolutionEvent[]
  capabilities: Capabilities
}

/** 首页(/api/home):只回答"能不能工作 / 帮上忙了吗 / 要我做什么"需要的数字。 */
export interface HomeSummary {
  storage: { postgresql: boolean; qdrant: boolean }
  capabilities: Capabilities
  /** 判定记录关着时不记注入与反馈,"帮上忙了吗"没有数可算。 */
  decision_log: boolean
  /** 最近 days 天:真实提问的判定数、注入的记忆条数、被证实有用 / 说反了的主反馈数。 */
  impact: { days: number; primes: number; injected: number; helped: number; harmed: number }
  /** 最近 14 天按看的人所在时区切日。 */
  series: { day: string; injected: number; helped: number }[]
  last_prime_at: string | null
  last_consolidation_at: string | null
  pending_review: number
  failed_jobs: number
}

export interface Metrics {
  feedback_coverage: number
  activated_enzymes: number
  activated_and_reported: number
  activation_weighted_precision: number | null
  false_positive_rate: number | null
  by_status: Record<string, number>
  hebbian_30d: { reinforcing: number; corrective: number; negations_learned: number }
  speculative_active: number
  replay_30d: { total: number; helped: number; contradicted: number }
}

// ── 巩固 / 调度 ─────────────────────────────────────────

export interface Schedule {
  interval: number
  enabled: boolean
  dreamglia_enabled: boolean
  dreamglia_max_dreams: number
  mutation_enabled: boolean
  crossover_enabled: boolean
  replay_enabled: boolean
  replay_max_episodes: number
}

/** 流程页用的实际参数(/api/flow):运行参数原值、质量门分数线、定时巩固的间隔与开关。 */
export interface FlowFacts {
  settings: Record<string, number | boolean | string>
  quality: { pass: number; refine: number; quarantine: number }
  schedule: Schedule & { running: boolean; due_in: number }
}

export interface ConsolidationJob {
  running: boolean
  status: "pending" | "running" | "succeeded" | "failed" | null
  job_id: string | null
  last_run: string | null
  last_result: Record<string, unknown> | null
}

export interface Consolidation {
  schedule: Schedule
  job: ConsolidationJob
}

// ── 园丁 ───────────────────────────────────────────────

export interface GardenerConfig {
  mode: "local" | "docker"
  model_id: string
  model: string
  base_url: string
  auth_token: string
  repo_path: string
  api_url: string
  api_token: string
  max_enzymes: number
  timeout_minutes: number
  allowed_tools: string
  docker_image: string
  docker_host: string
  nightly_enabled: boolean
  nightly_hour: number
}

export interface GardenerStatus {
  running: boolean
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  error: string
  log_lines: number
}

export interface GardenerLastRun {
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  error: string
  trigger: string
  stopped: boolean
  log_tail: string[]
}

export interface GardenerState {
  config: GardenerConfig
  status: GardenerStatus
  last_run: GardenerLastRun | null
  command: string
  prompt_exists: boolean
}

export interface GardenerLog extends GardenerStatus {
  offset: number
  lines: string[]
  last_run?: GardenerLastRun | null
}

export interface GardenerCheck {
  key: string
  label: string
  ok: boolean
  detail: string
}

// ── 安全 / 接入 ────────────────────────────────────────

export type TokenKind = "api" | "mcp"

export interface Security {
  /** 后端实际监听的端口(CE_API_PORT / CE_MCP_PORT),接入片段按它生成 */
  ports?: { api: number; mcp: number }
  auth_enabled: boolean
  api_token: string
  mcp_token: string
  mcp_auth_active: boolean
  hook_script: string
  restart_required?: boolean
}

// ── 运行参数 ────────────────────────────────────────────

export type SettingType =
  | "bool"
  | "int"
  | "float"
  | "enum"
  | "str"
  | "list"
  | "json"
  | "model"
  | "models"

export type SettingValue = boolean | number | string | string[] | Record<string, unknown>

export interface SettingGroup {
  key: string
  label: string
  description: string
}

export interface SettingItem {
  key: string
  group: string
  type: SettingType
  label: string
  help: string
  default: SettingValue
  value: SettingValue
  source: "db" | "env" | "default"
  env: string
  env_value: SettingValue | null
  choices: { value: string; label: string }[]
  min: number | null
  max: number | null
  unit: string
  restart: boolean
  advanced: boolean
  kinds: ModelKind[]
  updated_at: string | null
}

export interface SettingsSnapshot {
  groups: SettingGroup[]
  settings: SettingItem[]
  restart_required: string[]
  changed?: string[]
}

// ── 模型注册表 ──────────────────────────────────────────

export type Protocol = "openai" | "anthropic" | "system_one"
export type ModelKind = "chat" | "embedding" | "rerank" | "judge"

export interface Provider {
  id: string
  name: string
  protocol: Protocol
  base_url: string
  api_key_set: boolean
  api_key_hint: string
  headers: Record<string, string>
  enabled: boolean
  note: string
  models: string[]
  created_at: string
  updated_at: string
}

export interface ProviderInput {
  id: string
  name?: string
  protocol: Protocol
  base_url: string
  api_key?: string
  headers?: Record<string, string>
  enabled?: boolean
  note?: string
}

export interface ProviderPatch {
  name?: string
  protocol?: Protocol
  base_url?: string
  api_key?: string | null
  clear_api_key?: boolean
  headers?: Record<string, string>
  enabled?: boolean
  note?: string
}

export interface ModelParams {
  temperature?: number | null
  max_tokens?: number
  timeout?: number
  concurrency?: number
  retries?: number
  api_version?: string
  extra?: Record<string, unknown>
  dimensions?: number
  top_n?: number
}

export interface ModelEntry {
  id: string
  provider_id: string
  provider_name: string
  protocol: Protocol | ""
  kind: ModelKind
  model: string
  params: ModelParams
  enabled: boolean
  note: string
  used_by: string[]
  created_at: string
  updated_at: string
}

export interface ModelInput {
  id: string
  provider_id: string
  kind: ModelKind
  model: string
  params?: ModelParams
  enabled?: boolean
  note?: string
}

export type ModelPatch = Partial<Omit<ModelInput, "id">>

export interface ProbeResult {
  ok: boolean
  latency_ms: number
  detail: string
  models?: string[]
  response?: string
  dimensions?: number
  scores?: (number | null)[]
  answers?: Record<string, unknown>
  model?: string
}

export interface RoleStatus {
  key: string
  label: string
  help: string
  kinds: ModelKind[]
  advanced: boolean
  model_id: string
  source: "db" | "env" | "none" | "inherit"
  model?: string
  url?: string
  provider?: string
  error?: string
}

export interface RolesResponse {
  roles: RoleStatus[]
  restart_required: string[]
}

export interface ImportEnvResult {
  created: string[]
  bindings: Record<string, string>
  teachers: string[]
}

// ── 审计 / 任务 / 系统 ──────────────────────────────────

export interface Paged<T> {
  items: T[]
  total: number
}

export interface AuditEntry {
  id: number
  actor: string
  action: string
  target: string
  detail: Record<string, unknown>
  created_at: string
}

export interface Job {
  id: string
  kind: string
  status: "pending" | "running" | "succeeded" | "failed"
  attempts: number
  result: Record<string, unknown>
  error: string
  created_at: string
  updated_at: string
  finished_at: string | null
  trigger: string | null
  session_id: string | null
  project: string | null
  transcript_chars: number | null
}

export interface JobsResponse extends Paged<Job> {
  counts: Record<string, Record<string, number>>
}

export interface SystemStatus {
  version: string
  python: string
  platform: string
  started_at: string
  uptime_s: number
  checks: { postgresql: boolean; qdrant: boolean; mcp: boolean; workers: boolean }
  pg_version?: string
  schema_version?: number
  vector_sync_pending?: number
  jobs?: Record<string, Record<string, number>>
  tables?: Record<string, { rows: number; bytes: number }>
  qdrant?: { url: string; collection: string; documents: number }
  postgresql_error?: string
  qdrant_error?: string
  settings: { db: number; env: number; default: number }
  registry: { providers: number; models: number }
  capabilities: Capabilities
  auth: { enabled: boolean; api_token_set: boolean; mcp_token_set: boolean }
  restart_required: string[]
}

// ── 判定记录 ────────────────────────────────────────────

export interface DistillStats {
  decisions: number
  last_24h: number
  by_source: Record<string, number>
  by_judge: Record<string, number>
  outcomes: Record<string, number>
  decisions_with_direct_outcomes: number
  teacher_labels: Record<string, number>
  storage_mb: number
  archive: {
    enabled: boolean
    dir: string
    hours: number
    keep_days: number
    state?: Record<string, unknown>
    files?: number
    bytes?: number
  }
  decision_log: boolean
  shadow_judges: string[]
}

export interface DecisionRow {
  id: string
  created_at: string
  session_id: string
  project: string
  domain: string
  source: string
  judge: string
  model: string
  state: string
  candidates: number
  injected: number
  outcomes: number
  labels: number
}

export interface DecisionCandidate {
  id: string
  version: number | null
  key: string
  recall_rank: number | null
  rerank: number | null
  tier: string | null
  rescued: boolean
  hint: number | null
  votes: number
  lexical: boolean
  p: number | null
  q: number | null
  analog_p: number | null
  analog_q: number | null
}

export interface DecisionDetail {
  id: string
  created_at: string
  session_id: string
  project: string
  domain: string
  context_hash: string
  policy: Record<string, unknown>
  request: { state: string; questions: Record<string, unknown> } | null
  answers: Record<string, unknown> | null
  candidates: DecisionCandidate[]
  injected: { id: string; reason: string; exploratory: boolean }[]
  outcomes: { enzyme_id: string; outcome: string; source: string; created_at: string }[]
  teacher_labels: { teacher: string; answers: Record<string, unknown>; usage: Record<string, unknown>; created_at: string }[]
}

// ── 召回调试台 ──────────────────────────────────────────

export interface PlaygroundCandidate {
  id: string
  patterns: string
  then: string
  domain: string
  severity: string
  confidence: number | null
  recall_rank: number | null
  lexical: boolean
  hint: number | null
  condition_killed: boolean
  rerank: number | null
  floor_passed: boolean | null
  score: number | null
  tier: string | null
  p: number | null
  q: number | null
  analog_p: number | null
  analog_q: number | null
  injected: boolean
  reason: string
  exploratory: boolean
  hook_filtered: boolean
}

export interface PlaygroundResult {
  query: string
  elapsed_ms: number
  status: {
    judge: string
    judge_model: string
    none: number | null
    analogy_none: number | null
    llm_judge: string
    rerank_failed: boolean
    expansion: string
    judge_wait_ms: number | null
    /** 领域分流:没分流(关闭 / 指定了领域 / 没配判官)时为 null */
    route?: {
      mode: string
      domains: [string, number][]
      none: number | null
      added_ids: string[]
      status: "ok" | "skipped"
      wait_ms: number
    } | null
  }
  stages: {
    recalled: number
    lexical: number
    condition_killed: number
    reranked: number | null
    scored: number
    judged: number
    injected: number
  }
  candidates: PlaygroundCandidate[]
}
