"use client"

import { RefreshCwIcon } from "lucide-react"

import { ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { useIsAdmin } from "@/lib/api/hooks"
import { formatNumber } from "@/lib/format"
import { cn } from "@/lib/utils"

import { useDashboardRange } from "../hooks/use-dashboard-range"
import { ActivityChart } from "./activity-chart"
import { CompositionCard } from "./composition-card"
import { KpiTiles } from "./kpi-tiles"
import { LearningHealth } from "./learning-health"
import { MatureList } from "./mature-list"
import { RecentEvents } from "./recent-events"

const timeFormat = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })

/**
 * 详细指标:学习环的全部数字。首页只回答「能不能工作 / 帮上忙了吗 / 要我做什么」,
 * 这页留给要看全部数字的时候。整页一个 @container,网格按自身宽度而不是视口折行。
 */
export function OverviewDashboard() {
  const isAdmin = useIsAdmin()
  const { query, data, range, setRange, current, previous } = useDashboardRange()
  const today = data?.activity.at(-1)

  if (query.isError && !data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} title="指标加载失败" />

  return (
    <div className="@container flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground tabular-nums">
          {data ? (
            <>
              <span>{formatNumber(data.totals.enzymes)} 条记忆</span>
              <span>向量索引 {formatNumber(data.totals.vectors)} 条</span>
              {today?.decisions ? <span>今日 prime {formatNumber(today.decisions)} 次</span> : null}
              {query.dataUpdatedAt ? <span>更新于 {timeFormat.format(query.dataUpdatedAt)}</span> : null}
            </>
          ) : null}
        </p>
        <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
          <RefreshCwIcon className={cn(query.isFetching && "animate-spin")} aria-hidden="true" />
          刷新
        </Button>
      </div>

      <KpiTiles data={data} />

      <div className="grid grid-cols-1 gap-4 @5xl:grid-cols-3">
        <ActivityChart
          className="@5xl:col-span-2"
          current={current}
          previous={previous}
          range={range}
          onRangeChange={setRange}
          loading={!data}
          refreshing={query.isPlaceholderData}
        />
        <LearningHealth data={data} isAdmin={isAdmin} />
        <CompositionCard data={data} />
        <MatureList data={data} />
        <RecentEvents data={data} />
      </div>
    </div>
  )
}
