"use client"

// 记忆相关的查询与写操作。写操作成功后统一失效列表、分面(侧栏角标读它)、首页、详细指标与单条详情。
import { useCallback } from "react"
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { dashboardApi, enzymeApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { BulkAction, EnzymeInput, EnzymeListParams, EnzymeUpdate, PatternItem } from "@/lib/api/types"

export function useEnzymeList(params: EnzymeListParams, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: qk.enzymes(params),
    queryFn: () => enzymeApi.list(params),
    placeholderData: keepPreviousData,
    enabled: options?.enabled,
  })
}

/**
 * 记忆页的信息流:按筛选一次取一批,滚到底再取下一批(offset 分页)。
 * 键挂在 enzymes 前缀下,写操作失效列表时一起刷新。
 */
export function useEnzymeFeed(params: Omit<EnzymeListParams, "limit" | "offset">, pageSize = 20) {
  return useInfiniteQuery({
    queryKey: [...qk.enzymes(params), "feed", pageSize],
    queryFn: ({ pageParam }) => enzymeApi.list({ ...params, limit: pageSize, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, page) => n + page.enzymes.length, 0)
      return last.enzymes.length > 0 && loaded < last.total ? loaded : undefined
    },
    placeholderData: keepPreviousData,
  })
}

export function useEnzymeFacets() {
  return useQuery({ queryKey: qk.facets, queryFn: enzymeApi.facets })
}

/** 全库的平均置信度与累计触发 / 激活:只有总览接口给得出,放在 enzymes 前缀下随写操作一起失效。 */
export function useEnzymeTotals() {
  return useQuery({
    queryKey: [...qk.enzymes(), "totals"],
    queryFn: async () => (await dashboardApi.get(1)).totals,
    staleTime: 30_000,
  })
}

export function useEnzymeDetail(id: string | null) {
  return useQuery({
    queryKey: qk.enzyme(id ?? ""),
    queryFn: () => enzymeApi.get(id as string),
    enabled: Boolean(id),
  })
}

export function useEnzymeEvolution(id: string | null, limit = 20) {
  const params = { enzyme_id: id ?? "", limit }
  return useQuery({
    queryKey: qk.evolution(params),
    queryFn: () => enzymeApi.evolution(params),
    select: (data) => data.events,
    enabled: Boolean(id),
  })
}

export function useInvalidateEnzymes() {
  const client = useQueryClient()
  return useCallback(
    (options: { touched?: string[]; removed?: string[] } = {}) => {
      // 只清掉没人在看的详情缓存:正开着的抽屉若被清掉会立刻重取、闪一下「找不到」,
      // 它会在关闭或切走之后按过期规则回收
      for (const id of options.removed ?? []) client.removeQueries({ queryKey: qk.enzyme(id), type: "inactive" })
      return Promise.all([
        client.invalidateQueries({ queryKey: qk.enzymes() }),
        client.invalidateQueries({ queryKey: qk.facets }),
        client.invalidateQueries({ queryKey: qk.dashboard }),
        client.invalidateQueries({ queryKey: qk.home }),
        client.invalidateQueries({ queryKey: qk.evolution() }),
        client.invalidateQueries({ queryKey: qk.audit() }),
        ...(options.touched ?? []).map((id) => client.invalidateQueries({ queryKey: qk.enzyme(id) })),
      ])
    },
    [client],
  )
}

export function useUpdateEnzyme() {
  const invalidate = useInvalidateEnzymes()
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: EnzymeUpdate }) => enzymeApi.update(id, data),
    onSuccess: (_result, { id }) => invalidate({ touched: [id] }),
  })
}

export function useBulkEnzymes() {
  const invalidate = useInvalidateEnzymes()
  return useMutation({
    mutationFn: ({ ids, action }: { ids: string[]; action: BulkAction }) => enzymeApi.bulk(ids, action),
    onSuccess: (result) =>
      result.action === "delete"
        ? invalidate({ removed: [...result.done, ...result.missing] })
        : invalidate({ touched: result.done }),
  })
}

export function useDeleteEnzyme() {
  const invalidate = useInvalidateEnzymes()
  return useMutation({
    mutationFn: (id: string) => enzymeApi.remove(id),
    onSuccess: (_result, id) => invalidate({ removed: [id] }),
  })
}

/**
 * 新建接口只收逗号分隔的 `when` 字符串,含逗号的正则(如 `\d{1,3}`)会被拆坏。
 * 这类条目先不放进 `when`,建好后再用编辑接口写入完整的触发条件列表。
 */
export function useCreateEnzyme() {
  const invalidate = useInvalidateEnzymes()
  return useMutation({
    mutationFn: async (input: Omit<EnzymeInput, "when"> & { patterns: PatternItem[] }) => {
      const { patterns, ...rest } = input
      const safe = patterns.filter((item) => !item.value.includes(","))
      const when = (safe.length ? safe : [{ type: "keyword" as const, value: patterns[0].value.replaceAll(",", " ") }])
        .map((item) => (item.type === "regex" ? `regex:${item.value}` : item.value))
        .join(", ")
      const created = await enzymeApi.create({ ...rest, when })
      if (safe.length !== patterns.length) await enzymeApi.update(created.id, { patterns })
      return created
    },
    onSuccess: (created) => invalidate({ touched: [created.id] }),
  })
}
