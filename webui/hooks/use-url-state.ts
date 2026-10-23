"use client"

import { useCallback } from "react"
import { usePathname, useSearchParams } from "next/navigation"

type Patch = Record<string, string | number | boolean | null | undefined>

/**
 * 过滤、分页与打开的详情放在 URL 里:刷新、分享都还在。
 * 写 URL 用 history.replaceState:Next 会把它同步给 useSearchParams,又不会为搜索框每个按键发一次服务端请求。
 */
export function useUrlState() {
  const params = useSearchParams()
  const pathname = usePathname()
  const update = useCallback(
    (patch: Patch) => {
      const next = new URLSearchParams(window.location.search)
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || value === "" || value === false) next.delete(key)
        else next.set(key, String(value))
      }
      const query = next.toString()
      window.history.replaceState(null, "", query ? `${pathname}?${query}` : pathname)
    },
    [pathname],
  )
  return [params, update] as const
}

/** URL 里的非负整数参数,不合法时回落到默认值。 */
export function intParam(value: string | null, fallback: number, allowed?: readonly number[]) {
  const n = Number.parseInt(value ?? "", 10)
  if (!Number.isFinite(n) || n < 0) return fallback
  if (allowed && !allowed.includes(n)) return fallback
  return n
}

/** 一个查询参数当作状态用。 */
export function useSearchParamState(key: string, fallback = ""): [string, (value: string) => void] {
  const [params, update] = useUrlState()
  const value = params.get(key) ?? fallback
  const set = useCallback((next: string) => update({ [key]: next === fallback ? null : next }), [fallback, key, update])
  return [value, set]
}
