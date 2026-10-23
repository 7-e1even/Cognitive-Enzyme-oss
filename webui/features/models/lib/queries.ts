"use client"

import { useQuery } from "@tanstack/react-query"

import { adminApi } from "@/lib/api/endpoints"

/**
 * 服务商连通性测试的结果放进查询缓存,而不是某一行的局部状态:行里的结果面板、
 * 「新建模型」表单里的模型名候选读的是同一份,测一次两处都有。
 * 不挂在 ["admin", …] 下面,别人按前缀失效 admin 缓存时不会顺手把它重跑一遍。
 */
export const probeKey = (providerId: string) => ["provider-probe", providerId] as const

export function useProviderProbe(providerId: string | null | undefined) {
  return useQuery({
    queryKey: probeKey(providerId ?? ""),
    queryFn: () => adminApi.testProvider(providerId as string),
    // 只在点「测试」时发请求:一次探测就是一次真实的外部调用
    enabled: false,
    retry: false,
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  })
}
