// 后端接口函数,按资源分组;页面通过 lib/api/hooks.ts 的 React Query 钩子调用。
import { api, request } from "./client"
import type {
  AdminUser,
  AuditEntry,
  BulkAction,
  Consolidation,
  Dashboard,
  DecisionDetail,
  DecisionRow,
  DistillStats,
  EnzymeDetail,
  EnzymeFacets,
  EnzymeInput,
  EnzymeListParams,
  EnzymeRow,
  EnzymeUpdate,
  EvolutionEvent,
  FlowFacts,
  GardenerCheck,
  GardenerConfig,
  GardenerLog,
  GardenerState,
  Health,
  HomeSummary,
  ImportEnvResult,
  JobsResponse,
  Metrics,
  ModelEntry,
  ModelInput,
  ModelPatch,
  Paged,
  PlaygroundResult,
  ProbeResult,
  Provider,
  ProviderInput,
  ProviderPatch,
  RolesResponse,
  Schedule,
  SearchHit,
  Security,
  SettingsSnapshot,
  SettingValue,
  SystemStatus,
  TokenKind,
  User,
} from "./types"

export const authApi = {
  status: () => request<{ auth_required: boolean }>("/api/auth/status", { noRedirect: true }),
  login: (username: string, password: string) =>
    request<{ auth_required: boolean; token: string; user: User }>("/api/auth/login", {
      method: "POST",
      body: { username, password },
      noRedirect: true,
    }),
  me: () => api.get<User>("/api/auth/me"),
  updateProfile: (display_name: string) => api.patch<User>("/api/auth/me", { display_name }),
  changePassword: (old_password: string, new_password: string) =>
    api.post<{ ok: boolean }>("/api/auth/me/password", { old_password, new_password }),
  uploadAvatar: (file: File) => {
    const form = new FormData()
    form.append("file", file)
    return request<User>("/api/auth/me/avatar", { method: "POST", body: form })
  },
}

export const healthApi = {
  get: () => request<Health>("/api/health", { noRedirect: true }),
}

export const dashboardApi = {
  get: (days = 14) => api.get<Dashboard>("/api/dashboard", { days }),
  metrics: () => api.get<Metrics>("/api/metrics"),
  home: (tz: string) => api.get<HomeSummary>("/api/home", { tz }),
  flow: () => api.get<FlowFacts>("/api/flow"),
}

export const enzymeApi = {
  list: (params: EnzymeListParams) =>
    api.get<{ enzymes: EnzymeRow[]; total: number }>("/api/enzymes", { ...params }),
  facets: () => api.get<EnzymeFacets>("/api/enzymes/facets"),
  get: (id: string) => api.get<EnzymeDetail>(`/api/enzymes/${encodeURIComponent(id)}`),
  create: (data: EnzymeInput) =>
    api.post<{ id: string; status: string; confidence: number }>("/api/enzymes", data),
  update: (id: string, data: EnzymeUpdate) =>
    api.put<{ status: string; id: string }>(`/api/enzymes/${encodeURIComponent(id)}`, data),
  remove: (id: string) => api.delete<{ status: string; id: string }>(`/api/enzymes/${encodeURIComponent(id)}`),
  bulk: (ids: string[], action: BulkAction) =>
    api.post<{ action: string; done: string[]; missing: string[] }>("/api/enzymes/bulk", { ids, action }),
  search: (q: string, domain?: string, limit = 20) =>
    api.get<{ hits: SearchHit[]; total: number }>("/api/search", { q, domain, limit }),
  evolution: (params: { enzyme_id?: string; event_type?: string; limit?: number }) =>
    api.get<{ events: EvolutionEvent[] }>("/api/evolution", params),
}

export const consolidationApi = {
  get: () => api.get<Consolidation>("/api/consolidation"),
  updateSchedule: (data: Partial<Schedule>) => api.put<Consolidation>("/api/consolidation/schedule", data),
  run: () => api.post<Consolidation>("/api/consolidation/run"),
}

export const gardenerApi = {
  get: () => api.get<GardenerState>("/api/gardener"),
  updateConfig: (data: Partial<GardenerConfig>) =>
    api.put<{ config: GardenerConfig; command: string; message: string }>("/api/gardener/config", data),
  run: () => api.post<{ status: string; message: string }>("/api/gardener/run"),
  stop: () => api.post<{ status: string; message: string }>("/api/gardener/stop"),
  log: (offset: number) => api.get<GardenerLog>("/api/gardener/log", { offset }),
  check: () => api.get<{ ok: boolean; checks: GardenerCheck[] }>("/api/gardener/check"),
}

export const adminApi = {
  settings: () => api.get<SettingsSnapshot>("/api/admin/settings"),
  updateSettings: (values: Record<string, SettingValue>, reset: string[] = []) =>
    api.patch<SettingsSnapshot>("/api/admin/settings", { values, reset }),

  providers: () => api.get<{ providers: Provider[] }>("/api/admin/providers"),
  createProvider: (data: ProviderInput) => api.post<Provider>("/api/admin/providers", data),
  updateProvider: (id: string, data: ProviderPatch) =>
    api.patch<Provider>(`/api/admin/providers/${encodeURIComponent(id)}`, data),
  deleteProvider: (id: string, force = false) =>
    api.delete<{ status: string; models: string[] }>(`/api/admin/providers/${encodeURIComponent(id)}`, {
      force: force || undefined,
    }),
  testProvider: (id: string) => api.post<ProbeResult>(`/api/admin/providers/${encodeURIComponent(id)}/test`),

  models: () => api.get<{ models: ModelEntry[] }>("/api/admin/models"),
  createModel: (data: ModelInput) => api.post<ModelEntry>("/api/admin/models", data),
  updateModel: (id: string, data: ModelPatch) =>
    api.patch<ModelEntry>(`/api/admin/models/${encodeURIComponent(id)}`, data),
  deleteModel: (id: string, force = false) =>
    api.delete<{ status: string; unbound: string[] }>(`/api/admin/models/${encodeURIComponent(id)}`, {
      force: force || undefined,
    }),
  testModel: (id: string, prompt = "") =>
    api.post<ProbeResult>(`/api/admin/models/${encodeURIComponent(id)}/test`, { prompt }),
  roles: () => api.get<RolesResponse>("/api/admin/models/roles"),
  bindRoles: (bindings: Record<string, string>) => api.put<RolesResponse>("/api/admin/models/roles", { bindings }),
  importEnv: () => api.post<ImportEnvResult>("/api/admin/models/import-env"),

  users: () => api.get<{ users: AdminUser[] }>("/api/admin/users"),
  createUser: (data: { username: string; password: string; display_name?: string; role: string }) =>
    api.post<AdminUser>("/api/admin/users", data),
  updateUser: (
    id: string,
    data: { display_name?: string; role?: string; is_active?: boolean; password?: string },
  ) => api.patch<AdminUser>(`/api/admin/users/${encodeURIComponent(id)}`, data),
  deleteUser: (id: string) => api.delete<{ status: string }>(`/api/admin/users/${encodeURIComponent(id)}`),

  audit: (params: { limit?: number; offset?: number; action?: string; actor?: string; q?: string }) =>
    api.get<Paged<AuditEntry>>("/api/admin/audit", params),

  jobs: (params: { kind?: string; status?: string; limit?: number; offset?: number }) =>
    api.get<JobsResponse>("/api/admin/jobs", params),
  retryJob: (id: string) => api.post<{ status: string }>(`/api/admin/jobs/${encodeURIComponent(id)}/retry`),

  system: () => api.get<SystemStatus>("/api/admin/system"),
  reindex: () => api.post<{ queued: number }>("/api/admin/system/reindex"),
  reload: () => api.post<{ status: string; restart_required: string[] }>("/api/admin/system/reload"),

  distillStats: () => api.get<DistillStats>("/api/admin/distill/stats"),
  archiveNow: () => api.post<Record<string, unknown>>("/api/admin/distill/archive"),
  decisions: (params: { limit?: number; offset?: number; source?: string; judge?: string }) =>
    api.get<Paged<DecisionRow>>("/api/admin/distill/decisions", params),
  decision: (id: string) => api.get<DecisionDetail>(`/api/admin/distill/decisions/${encodeURIComponent(id)}`),

  security: () => api.get<Security>("/api/admin/security"),
  updateSecurity: (auth_enabled: boolean) => api.patch<Security>("/api/admin/security", { auth_enabled }),
  setToken: (kind: TokenKind, token: string) =>
    api.put<Security>(`/api/admin/security/tokens/${kind}`, { token }),
  rotateToken: (kind: TokenKind) => api.post<Security>(`/api/admin/security/tokens/${kind}/rotate`),
  clearToken: (kind: TokenKind) => api.delete<Security>(`/api/admin/security/tokens/${kind}`),

  playground: (data: {
    context: string
    domain?: string
    project?: string
    max_results?: number
    hook_min_conf?: number
  }) => api.post<PlaygroundResult>("/api/admin/playground/prime", data),
}
