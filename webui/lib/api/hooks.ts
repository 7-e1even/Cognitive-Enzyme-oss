"use client"

// 共享的 React Query 钩子与缓存键。页面里少见的查询直接 useQuery + endpoints 即可。
import { useQuery } from "@tanstack/react-query"

import { adminApi, authApi, healthApi } from "./endpoints"

export const qk = {
  me: ["me"] as const,
  health: ["health"] as const,
  dashboard: ["dashboard"] as const,
  home: ["home"] as const,
  flow: ["flow"] as const,
  enzymes: (params?: unknown) => (params ? (["enzymes", params] as const) : (["enzymes"] as const)),
  enzyme: (id: string) => ["enzyme", id] as const,
  facets: ["enzyme-facets"] as const,
  evolution: (params?: unknown) => (params ? (["evolution", params] as const) : (["evolution"] as const)),
  consolidation: ["consolidation"] as const,
  gardener: ["gardener"] as const,
  settings: ["admin", "settings"] as const,
  providers: ["admin", "providers"] as const,
  models: ["admin", "models"] as const,
  roles: ["admin", "roles"] as const,
  users: ["admin", "users"] as const,
  audit: (params?: unknown) => (params ? (["admin", "audit", params] as const) : (["admin", "audit"] as const)),
  jobs: (params?: unknown) => (params ? (["admin", "jobs", params] as const) : (["admin", "jobs"] as const)),
  system: ["admin", "system"] as const,
  distill: ["admin", "distill"] as const,
  decisions: (params?: unknown) =>
    params ? (["admin", "decisions", params] as const) : (["admin", "decisions"] as const),
  security: ["admin", "security"] as const,
}

export function useMe() {
  return useQuery({ queryKey: qk.me, queryFn: authApi.me, staleTime: 60_000 })
}

export function useHealth() {
  return useQuery({ queryKey: qk.health, queryFn: healthApi.get, refetchInterval: 30_000 })
}

export function useIsAdmin() {
  const me = useMe()
  return me.data?.role === "admin"
}

export function useSystem(enabled = true) {
  return useQuery({ queryKey: qk.system, queryFn: adminApi.system, refetchInterval: 15_000, enabled })
}

export function useSettings() {
  return useQuery({ queryKey: qk.settings, queryFn: adminApi.settings })
}

export function useProviders() {
  return useQuery({ queryKey: qk.providers, queryFn: async () => (await adminApi.providers()).providers })
}

export function useModels() {
  return useQuery({ queryKey: qk.models, queryFn: async () => (await adminApi.models()).models })
}

export function useRoles() {
  return useQuery({ queryKey: qk.roles, queryFn: adminApi.roles })
}
