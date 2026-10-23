"use client"

import { useCallback } from "react"
import { usePathname, useSearchParams } from "next/navigation"

export const PAGE_SIZES = [20, 50, 100]

export interface DistillParams {
  source: string
  judge: string
  /** 从 1 开始 */
  page: number
  size: number
  /** 打开详情的判定 id,可以直接分享链接定位到某一条 */
  decision: string
}

const DEFAULTS: Record<keyof DistillParams, string | number> = {
  source: "",
  judge: "",
  page: 1,
  size: PAGE_SIZES[0],
  decision: "",
}

/**
 * 筛选、分页与打开的记录都放在 URL 里。用 history.replaceState 改地址(Next 会同步给
 * useSearchParams),不触发服务端请求,也不在历史里堆一串记录。
 */
export function useDistillParams() {
  const pathname = usePathname()
  const searchParams = useSearchParams()

  const size = Number(searchParams.get("size"))
  const params: DistillParams = {
    source: searchParams.get("source") ?? "",
    judge: searchParams.get("judge") ?? "",
    page: Math.max(1, Math.floor(Number(searchParams.get("page")) || 1)),
    size: PAGE_SIZES.includes(size) ? size : PAGE_SIZES[0],
    decision: searchParams.get("decision") ?? "",
  }

  const setParams = useCallback(
    (patch: Partial<DistillParams>) => {
      const next = new URLSearchParams(window.location.search)
      const filterChanged = patch.source !== undefined || patch.judge !== undefined || patch.size !== undefined
      const merged: Partial<DistillParams> = { ...patch }
      if (filterChanged && patch.page === undefined) merged.page = 1
      for (const [key, value] of Object.entries(merged) as [keyof DistillParams, string | number][]) {
        if (value === undefined) continue
        if (value === "" || value === DEFAULTS[key]) next.delete(key)
        else next.set(key, String(value))
      }
      const qs = next.toString()
      window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname)
    },
    [pathname],
  )

  return { params, setParams }
}
