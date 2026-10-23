"use client"

import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { format } from "date-fns"
import { DownloadIcon, FunnelXIcon, GitBranchIcon, RefreshCwIcon } from "lucide-react"
import { useMemo } from "react"

import { OptionSelect } from "@/components/option-select"
import { EmptyState, ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { intParam, useUrlState } from "@/hooks/use-url-state"
import { enzymeApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { EvolutionEvent } from "@/lib/api/types"
import { downloadJson } from "@/lib/download"
import { formatNumber } from "@/lib/format"
import { EVENT_TYPE, labelOf } from "@/lib/labels"

import { dayKey, dayTitle, EVENT_TYPE_ORDER } from "../lib/events"
import { DayGroup } from "./day-group"

const LIMITS = [50, 100, 200, 500] as const
/** 用来把事件里的酶 ID 换成建议原文:取最近改动过的一批,进化事件涉及的记忆大都在里面 */
const NAME_WINDOW = { limit: 500, sort: "updated_at", order: "desc" } as const

/** 进化事件:每一次记忆变化(反馈学习、沉淀、巩固、梦境、免疫、回放…)按天分组排成时间线。类型与条数写进 URL。 */
export function EvolutionView() {
  const [params, setParams] = useUrlState()
  const type = params.get("type") ?? ""
  const limit = intParam(params.get("limit"), 100, LIMITS)

  const request = { event_type: type || undefined, limit }
  const events = useQuery({
    queryKey: qk.evolution(request),
    queryFn: () => enzymeApi.evolution(request),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  })
  const enzymes = useQuery({ queryKey: qk.enzymes(NAME_WINDOW), queryFn: () => enzymeApi.list(NAME_WINDOW), staleTime: 60_000 })
  const names = useMemo(() => new Map((enzymes.data?.enzymes ?? []).map((enzyme) => [enzyme.id, enzyme.then])), [enzymes.data])

  const list = useMemo(() => events.data?.events ?? [], [events.data])
  const days = useMemo(() => {
    const groups = new Map<string, EvolutionEvent[]>()
    for (const event of list) {
      const key = dayKey(event.created_at)
      const bucket = groups.get(key)
      if (bucket) bucket.push(event)
      else groups.set(key, [event])
    }
    return [...groups.entries()]
  }, [list])
  const typeCounts = useMemo(() => {
    const counts = new Map<string, number>()
    for (const event of list) counts.set(event.event_type, (counts.get(event.event_type) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [list])
  const enzymeCount = useMemo(() => new Set(list.map((event) => event.enzyme_id).filter(Boolean)).size, [list])

  const knownTypes = [...EVENT_TYPE_ORDER, ...Object.keys(EVENT_TYPE).filter((key) => !EVENT_TYPE_ORDER.includes(key))]
  if (type && !knownTypes.includes(type)) knownTypes.push(type)
  const typeLabel = type ? labelOf.event(type).label : ""
  const reachedLimit = list.length >= limit

  if (events.isError && !events.data) return <ErrorState error={events.error} onRetry={() => void events.refetch()} />

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <OptionSelect
          label="按事件类型筛选"
          value={type}
          allLabel="全部类型"
          options={knownTypes.map((key) => ({ value: key, label: labelOf.event(key).label }))}
          onChange={(value) => setParams({ type: value || null })}
          className="w-36"
        />
        <OptionSelect
          label="显示条数"
          value={String(limit)}
          options={LIMITS.map((value) => ({ value: String(value), label: `最近 ${value} 条` }))}
          onChange={(value) => setParams({ limit: value && value !== "100" ? value : null })}
          className="w-32"
        />
        {type ? (
          <Button type="button" size="sm" variant="ghost" onClick={() => setParams({ type: null })}>
            <FunnelXIcon /> 清除筛选
          </Button>
        ) : null}
        <p className="ml-1 hidden text-sm text-muted-foreground tabular-nums md:block">
          {events.data ? `${formatNumber(list.length)} 条${typeLabel ? `「${typeLabel}」` : ""}事件 · 涉及 ${formatNumber(enzymeCount)} 条记忆${days.length ? ` · 跨 ${days.length} 天` : ""}` : ""}
        </p>
        <div className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            type="button"
            variant="outline"
            disabled={!list.length}
            onClick={() =>
              downloadJson(
                `evolution-${type || "all"}-${format(new Date(), "yyyyMMdd-HHmm")}.json`,
                { exported_at: new Date().toISOString(), event_type: type || null, limit, events: list },
                `${list.length} 条进化事件`,
              )
            }
          >
            <DownloadIcon /> 导出
          </Button>
          <Button size="icon-sm" type="button" variant="outline" aria-label="刷新" disabled={events.isFetching} onClick={() => void events.refetch()}>
            <RefreshCwIcon className={events.isFetching ? "animate-spin" : undefined} />
          </Button>
        </div>
      </div>

      <Card className="gap-0 py-0">
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 border-b py-4">
          <div className="flex min-w-0 flex-col gap-1">
            <CardTitle>时间线</CardTitle>
            {reachedLimit ? <CardDescription>只显示最近 {limit} 条;更早的事件可以调大条数或按类型筛选查看</CardDescription> : null}
          </div>
          {typeCounts.length > 1 ? (
            <div className="flex flex-wrap items-center gap-1.5" aria-label="本页各类型条数">
              {typeCounts.map(([key, count]) => (
                <button key={key} type="button" className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring/50" onClick={() => setParams({ type: key })} title={`只看「${labelOf.event(key).label}」`}>
                  <ToneBadge tone="outline" className="cursor-pointer gap-1 font-normal transition-colors hover:bg-muted">
                    {labelOf.event(key).label}
                    <span className="text-muted-foreground tabular-nums">{formatNumber(count)}</span>
                  </ToneBadge>
                </button>
              ))}
            </div>
          ) : null}
        </CardHeader>
        {events.isPending ? (
          <div className="flex flex-col gap-3 p-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-9 w-full" />
            ))}
          </div>
        ) : days.length ? (
          days.map(([key, bucket]) => {
            const title = dayTitle(key)
            return <DayGroup key={key} title={title.primary} subtitle={title.secondary} events={bucket} names={names} />
          })
        ) : (
          <EmptyState
            icon={GitBranchIcon}
            title={type ? `还没有「${typeLabel}」事件` : "还没有进化事件"}
            description={type ? "换一个类型看看,或者清除筛选。" : "反馈学习、会话沉淀、巩固、梦境与免疫巡逻发生时,每一次记忆变化都会记在这里。"}
          />
        )}
      </Card>
    </div>
  )
}
