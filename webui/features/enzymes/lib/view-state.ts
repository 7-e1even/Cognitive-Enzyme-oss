"use client"

// 酶库的视图状态放在 URL 里:筛选、排序、分页、搜索、打开的记忆都能分享、刷新后原样恢复。
import { useCallback, useMemo } from "react"
import { useSearchParams } from "next/navigation"

import type { EnzymeListParams } from "@/lib/api/types"

import {
  ORIGIN_VALUES,
  PAGE_SIZES,
  SEVERITY_VALUES,
  SORT_KEYS,
  STATUS_VALUES,
  type SortKey,
} from "./options"

export interface EnzymeViewState {
  q: string
  domain: string
  status: string
  severity: string
  origin: string
  minConfidence: number | null
  sort: SortKey
  order: "asc" | "desc"
  /** 从 1 开始 */
  page: number
  size: number
  /** 详情抽屉里打开的记忆 */
  id: string | null
  /** 新建抽屉是否打开(`?new=1`,命令面板从这里进来) */
  create: boolean
}

export const DEFAULT_SORT: SortKey = "updated_at"
export const DEFAULT_ORDER = "desc"
export const DEFAULT_PAGE_SIZE = 20

/** 筛选相关的字段;「清除筛选」和新建后的复位只动这些。 */
export const CLEARED_FILTERS = {
  q: "",
  domain: "",
  status: "",
  severity: "",
  origin: "",
  minConfidence: null,
} satisfies Partial<EnzymeViewState>

function oneOf<T extends string>(values: readonly T[], raw: string | null): T | "" {
  return raw && (values as readonly string[]).includes(raw) ? (raw as T) : ""
}

export function parseViewState(params: Pick<URLSearchParams, "get">): EnzymeViewState {
  const rawConfidence = Number(params.get("min_conf"))
  const rawPage = Number.parseInt(params.get("page") ?? "", 10)
  const rawSize = Number.parseInt(params.get("size") ?? "", 10)

  return {
    q: params.get("q") ?? "",
    domain: params.get("domain") ?? "",
    status: oneOf(STATUS_VALUES, params.get("status")),
    severity: oneOf(SEVERITY_VALUES, params.get("severity")),
    origin: oneOf(ORIGIN_VALUES, params.get("origin")),
    minConfidence:
      params.get("min_conf") !== null && rawConfidence > 0 && rawConfidence <= 1 ? rawConfidence : null,
    sort: oneOf(SORT_KEYS, params.get("sort")) || DEFAULT_SORT,
    order: params.get("order") === "asc" ? "asc" : "desc",
    page: Number.isFinite(rawPage) && rawPage > 0 ? rawPage : 1,
    size: PAGE_SIZES.includes(rawSize) ? rawSize : DEFAULT_PAGE_SIZE,
    id: params.get("id") || null,
    create: params.get("new") === "1",
  }
}

/** 只写与默认值不同的参数,地址栏里留下的都是有意义的。 */
export function serializeViewState(state: EnzymeViewState): string {
  const params = new URLSearchParams()
  if (state.q.trim()) params.set("q", state.q.trim())
  if (state.domain) params.set("domain", state.domain)
  if (state.status) params.set("status", state.status)
  if (state.severity) params.set("severity", state.severity)
  if (state.origin) params.set("origin", state.origin)
  if (state.minConfidence !== null) params.set("min_conf", String(state.minConfidence))
  if (state.sort !== DEFAULT_SORT || state.order !== DEFAULT_ORDER) {
    params.set("sort", state.sort)
    params.set("order", state.order)
  }
  if (state.page > 1) params.set("page", String(state.page))
  if (state.size !== DEFAULT_PAGE_SIZE) params.set("size", String(state.size))
  if (state.id) params.set("id", state.id)
  if (state.create) params.set("new", "1")
  return params.toString()
}

export function toListParams(state: EnzymeViewState): EnzymeListParams {
  return {
    q: state.q.trim() || undefined,
    domain: state.domain || undefined,
    status: state.status || undefined,
    severity: state.severity || undefined,
    origin: state.origin || undefined,
    min_confidence: state.minConfidence ?? undefined,
    sort: state.sort,
    order: state.order,
    limit: state.size,
    offset: (state.page - 1) * state.size,
  }
}

export function hasActiveFilters(state: EnzymeViewState) {
  return Boolean(
    state.q.trim() || state.domain || state.status || state.severity || state.origin || state.minConfidence !== null
  )
}

/**
 * 读写 URL 里的视图状态。
 *
 * 写入走 `history.replaceState`:Next 会把它同步给 useSearchParams,但不会重新请求服务端组件,
 * 翻页、排序这类高频操作不产生路由往返,也不在历史里堆一串记录。每次写入都从当前地址读起,
 * 同一轮事件里连续的几次修改会叠加,不会互相覆盖。
 */
export function useEnzymeViewState() {
  const searchParams = useSearchParams()
  const state = useMemo(() => parseViewState(searchParams), [searchParams])

  const update = useCallback((patch: Partial<EnzymeViewState>) => {
    const current = parseViewState(new URLSearchParams(window.location.search))
    const query = serializeViewState({ ...current, ...patch })
    const url = `${window.location.pathname}${query ? `?${query}` : ""}`
    window.history.replaceState(null, "", url)
  }, [])

  return [state, update] as const
}
