"use client"

import { ChartAreaIcon, TableIcon, TrendingDownIcon, TrendingUpIcon } from "lucide-react"
import { useMemo, useState } from "react"
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts"

import { Hint } from "@/components/hint"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent } from "@/components/ui/chart"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { ActivityPoint } from "@/lib/api/types"
import { formatNumber, longDay, shortDay } from "@/lib/format"
import { cn } from "@/lib/utils"

import { ACTIVITY_SERIES, activityChartConfig, RANGE_OPTIONS, sumSeries, windowChange, type ActivityKey, type RangeValue } from "./data"

type View = "chart" | "table"

function SeriesTile({
  label,
  hint,
  color,
  total,
  change,
  range,
  visible,
  onToggle,
}: {
  label: string
  hint: string
  color: string
  total: number
  change: number | null
  range: RangeValue
  visible: boolean
  onToggle: () => void
}) {
  const up = change !== null && change >= 0
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-pressed={visible}
          aria-label={`${label} ${formatNumber(total)}${change !== null ? `,较前 ${range} 天${up ? "增加" : "减少"} ${Math.abs(change)}%` : ""}。${visible ? "点击隐藏" : "点击显示"}这条曲线`}
          onClick={onToggle}
          className="-m-1.5 flex min-w-0 items-center gap-2.5 rounded-lg p-1.5 text-left transition-colors outline-none hover:bg-muted/50 focus-visible:ring-3 focus-visible:ring-ring/30"
        >
          <span className="h-12 w-1 shrink-0 rounded-full transition-colors" style={{ backgroundColor: visible ? color : "var(--border)" }} aria-hidden="true" />
          <span className="flex min-w-0 flex-col gap-2">
            <span className={cn("truncate text-xs font-medium", visible ? "text-muted-foreground" : "text-muted-foreground/60 line-through")}>{label}</span>
            <span className="flex items-center gap-2">
              <span className={cn("text-base leading-none font-semibold tabular-nums", !visible && "text-muted-foreground")}>{formatNumber(total)}</span>
              {change !== null ? (
                <span
                  className={cn("inline-flex items-center gap-0.5 text-xs font-medium [&_svg]:size-3.5", up ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}
                  title={`较前 ${range} 天`}
                >
                  {up ? <TrendingUpIcon aria-hidden="true" /> : <TrendingDownIcon aria-hidden="true" />}
                  {Math.abs(change)}%
                </span>
              ) : null}
            </span>
          </span>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-64">
        {hint}
        <span className="block opacity-70">点击{visible ? "隐藏" : "显示"}这条曲线</span>
      </TooltipContent>
    </Tooltip>
  )
}

/** 近期活动:每条曲线一个指标块(兼做图例,点一下隐藏 / 显示),下面是面积图或表格。四条线互相独立,所以不叠加。 */
export function ActivityChart({
  current,
  previous,
  range,
  onRangeChange,
  loading,
  refreshing,
  className,
}: {
  current: ActivityPoint[]
  previous: ActivityPoint[]
  range: RangeValue
  onRangeChange: (range: RangeValue) => void
  loading: boolean
  /** 换了时间范围、新数据还在路上:保留旧图并调淡,不闪骨架。 */
  refreshing: boolean
  className?: string
}) {
  const [view, setView] = useState<View>("chart")
  const [hidden, setHidden] = useState<Set<ActivityKey>>(() => new Set())
  const totals = useMemo(
    () =>
      ACTIVITY_SERIES.map((series) => {
        const total = sumSeries(current, series.key)
        // 前一窗口数据不全(库比窗口年轻)时,缺的天按 0 计——比不上就不画箭头
        const before = previous.length ? sumSeries(previous, series.key) : 0
        return { ...series, total, change: windowChange(total, before) }
      }),
    [current, previous],
  )
  const empty = !loading && totals.every((series) => series.total === 0)

  const toggle = (key: ActivityKey) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      // 至少留一条,否则图上什么都没有、也看不出是被藏掉了
      else if (next.size < ACTIVITY_SERIES.length - 1) next.add(key)
      return next
    })
  }

  // 一点活动都没有时整张卡不出现:空图表不回答任何问题
  if (empty) return null

  return (
    <Card className={className}>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-1.5">
          近期活动
          <Hint label="近期活动">每天的 prime 判定、事后反馈、会话捕获与进化事件数。点上方的数字可以隐藏 / 显示对应曲线;百分比是与前一段同样长的时间相比。</Hint>
        </CardTitle>
        <div className="flex items-center gap-2">
          <ToggleGroup type="single" value={range} onValueChange={(value) => value && onRangeChange(value as RangeValue)} variant="outline" size="sm" spacing={0} aria-label="时间范围">
            {RANGE_OPTIONS.map((option) => (
              <ToggleGroupItem key={option.value} value={option.value} className="px-2.5 text-xs">
                {option.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <ToggleGroup type="single" value={view} onValueChange={(value) => value && setView(value as View)} variant="outline" size="sm" spacing={0} aria-label="显示方式">
            <ToggleGroupItem value="chart" aria-label="图表" title="图表">
              <ChartAreaIcon aria-hidden="true" />
            </ToggleGroupItem>
            <ToggleGroupItem value="table" aria-label="表格" title="表格">
              <TableIcon aria-hidden="true" />
            </ToggleGroupItem>
          </ToggleGroup>
        </div>
      </CardHeader>
      <CardContent className={cn("flex flex-col gap-5 transition-opacity", refreshing && "opacity-60")} aria-busy={refreshing || loading}>
        <div className="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
          {loading
            ? ACTIVITY_SERIES.map((series) => <Skeleton key={series.key} className="h-12" />)
            : totals.map((series) => (
                <SeriesTile
                  key={series.key}
                  label={series.label}
                  hint={series.hint}
                  color={activityChartConfig[series.key].color}
                  total={series.total}
                  change={series.change}
                  range={range}
                  visible={!hidden.has(series.key)}
                  onToggle={() => toggle(series.key)}
                />
              ))}
        </div>
        {loading ? (
          <Skeleton className="h-[260px] w-full" />
        ) : view === "table" ? (
          <div className="h-[260px] overflow-y-auto rounded-lg border">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-muted/50">
                <TableRow>
                  <TableHead className="text-xs">日期</TableHead>
                  {ACTIVITY_SERIES.map((series) => (
                    <TableHead key={series.key} className="text-right text-xs">
                      {series.label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {[...current].reverse().map((point) => (
                  <TableRow key={point.day}>
                    <TableCell className="text-xs text-muted-foreground tabular-nums">{longDay(point.day)}</TableCell>
                    {ACTIVITY_SERIES.map((series) => (
                      <TableCell key={series.key} className="text-right text-xs tabular-nums">
                        {formatNumber(point[series.key])}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <ChartContainer config={activityChartConfig} className="aspect-auto h-[260px] w-full">
            <AreaChart accessibilityLayer data={current} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="day" tickFormatter={shortDay} tickLine={false} axisLine={false} tickMargin={10} minTickGap={24} />
              <YAxis width={36} tickLine={false} axisLine={false} allowDecimals={false} domain={[0, (max: number) => Math.max(4, Math.ceil(max * 1.1))]} />
              <ChartTooltip content={<ChartTooltipContent labelFormatter={(value) => longDay(String(value))} />} />
              {/* 按固定顺序绘制:颜色跟着曲线走,隐藏其中几条也不会改色 */}
              {ACTIVITY_SERIES.filter((series) => !hidden.has(series.key)).map((series) => (
                <Area key={series.key} dataKey={series.key} name={series.label} type="monotone" stroke={`var(--color-${series.key})`} strokeWidth={2} fill={`var(--color-${series.key})`} fillOpacity={0.08} dot={false} />
              ))}
            </AreaChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
}
