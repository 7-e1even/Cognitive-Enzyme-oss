"use client"

// 审核队列的数据:隔离中的记忆(新到旧),以及每条是被什么送进隔离的。
import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"

import { enzymeApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { EnzymeListParams, EvolutionEvent } from "@/lib/api/types"

import { quarantineReason } from "@/features/enzymes/lib/events"
import { useEnzymeList } from "@/features/enzymes/lib/queries"

/** 队列一次取回的上限;超过时页面会说明只显示了最新的这些。 */
export const QUEUE_LIMIT = 500

export const QUEUE_PARAMS: EnzymeListParams = {
  status: "quarantine",
  sort: "created_at",
  order: "desc",
  limit: QUEUE_LIMIT,
  offset: 0,
}

export function useReviewQueue() {
  return useEnzymeList(QUEUE_PARAMS)
}

export interface QueueReason {
  /** 表格里的短标签 */
  short: string
  /** 完整说明(悬停与详情) */
  full: string
}

function shortReason(event: EvolutionEvent): string {
  const detail = event.detail ?? {}
  if (event.event_type === "harvest") {
    const score = (detail.quality as { score?: number } | undefined)?.score
    return typeof score === "number" ? `质量门 ${score.toFixed(1)} 分` : "质量门"
  }
  switch (detail.patrol_type) {
    case "zombie":
      return "免疫:精度过低"
    case "stale":
      return "免疫:长期未用"
    case "speculative_overflow":
      return "免疫:超出容量"
    case "contradiction":
      return "免疫:内容矛盾"
    default:
      return "免疫巡检"
  }
}

const EVENT_LIMIT = 500

/**
 * 隔离原因来自两类进化事件:沉淀时的质量门(harvest)与免疫巡检(immune)。
 * 两次查询覆盖整个队列,不必逐条去取;都没有记录的,多半是有人手动送审。
 */
export function useQuarantineReasons() {
  const harvest = useQuery({
    queryKey: qk.evolution({ event_type: "harvest", limit: EVENT_LIMIT }),
    queryFn: () => enzymeApi.evolution({ event_type: "harvest", limit: EVENT_LIMIT }),
    staleTime: 60_000,
  })
  const immune = useQuery({
    queryKey: qk.evolution({ event_type: "immune", limit: EVENT_LIMIT }),
    queryFn: () => enzymeApi.evolution({ event_type: "immune", limit: EVENT_LIMIT }),
    staleTime: 60_000,
  })

  const reasons = useMemo(() => {
    const events = [...(harvest.data?.events ?? []), ...(immune.data?.events ?? [])].sort((a, b) =>
      (b.created_at ?? "").localeCompare(a.created_at ?? ""),
    )
    const map = new Map<string, QueueReason>()
    for (const event of events) {
      if (!event.enzyme_id || map.has(event.enzyme_id)) continue
      const full = quarantineReason(event)
      if (full) map.set(event.enzyme_id, { short: shortReason(event), full })
    }
    return map
  }, [harvest.data, immune.data])

  return { reasons, loading: harvest.isLoading || immune.isLoading }
}

/** 等了多久:分钟 / 小时 / 天,审核队列只关心量级。 */
export function waitingFor(iso: string | null, now = Date.now()) {
  if (!iso) return "—"
  const minutes = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000))
  if (minutes < 1) return "刚刚"
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} 小时`
  return `${Math.round(hours / 24)} 天`
}

export function waitingDays(iso: string | null, now = Date.now()) {
  if (!iso) return 0
  return (now - new Date(iso).getTime()) / 86_400_000
}
