"use client"

import { useCallback, useMemo } from "react"
import { usePathname, useSearchParams } from "next/navigation"
import { keepPreviousData, useQuery } from "@tanstack/react-query"

import { dashboardApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"

import { parseRange, type RangeValue } from "../components/data"

/**
 * 首页和详细指标共用的看板数据,时间范围放在 URL 的 `?days=` 里。
 *
 * 活动曲线要和"前一段同样长的时间"比较,所以按两倍天数取数,前一半是上一窗口、
 * 后一半是本窗口;其余字段(总数、构成、成熟记忆……)与天数无关。
 */
export function useDashboardRange() {
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const range = parseRange(searchParams.get("days"))
  const days = Number(range)

  const query = useQuery({
    queryKey: [...qk.dashboard, { days: days * 2 }],
    queryFn: () => dashboardApi.get(days * 2),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  })
  const data = query.data

  // 时间范围写进 URL(替换当前历史记录):刷新、分享链接都保留,也不产生一串后退记录
  const setRange = useCallback(
    (next: RangeValue) => {
      const params = new URLSearchParams(searchParams.toString())
      if (next === "14") params.delete("days")
      else params.set("days", next)
      const qs = params.toString()
      window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname)
    },
    [pathname, searchParams],
  )

  // 同一份数据只切一次,图表拿到的数组引用保持稳定
  const { current, previous } = useMemo(() => {
    const activity = data?.activity ?? []
    return {
      current: activity.slice(-days),
      previous: activity.slice(0, Math.max(0, activity.length - days)),
    }
  }, [data, days])

  return { query, data, range, setRange, current, previous }
}
