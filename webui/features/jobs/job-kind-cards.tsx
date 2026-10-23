"use client"

import { BrainCircuitIcon, ListChecksIcon, MessagesSquareIcon, type LucideIcon } from "lucide-react"

import { Card, CardContent } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { KIND_HINT, STATUS_DOT, STATUS_TABS, type StatusTab } from "./lib/data"

const KIND_ICON: Record<string, LucideIcon> = {
  capture: MessagesSquareIcon,
  consolidate: BrainCircuitIcon,
}

/**
 * 每种任务一张卡:总数、成功率,以及各状态的小标签——点一下直接把列表筛到那一格。
 * 只列出有任务的类型,只显示有数的状态,没有空卡片和零。
 */
export function JobKindCards({
  counts,
  kinds,
  loading,
  activeKind,
  activeStatus,
  onPick,
}: {
  counts: Record<string, Record<string, number>>
  kinds: string[]
  loading: boolean
  activeKind: string
  activeStatus: StatusTab
  onPick: (kind: string, status: StatusTab) => void
}) {
  if (loading) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {Array.from({ length: 2 }, (_, i) => (
          <Skeleton key={i} className="h-32 w-full rounded-xl" />
        ))}
      </div>
    )
  }
  const present = kinds.filter((kind) => Object.values(counts[kind] ?? {}).some((n) => n > 0))
  if (present.length === 0) return null

  return (
    <div className={cn("grid grid-cols-1 gap-4", present.length > 1 && "md:grid-cols-2", present.length > 2 && "xl:grid-cols-3")}>
      {present.map((kind) => {
        const byStatus = counts[kind] ?? {}
        const total = Object.values(byStatus).reduce((a, b) => a + b, 0)
        const done = (byStatus.succeeded ?? 0) + (byStatus.failed ?? 0)
        const rate = done > 0 ? (byStatus.succeeded ?? 0) / done : null
        const label = labelOf.jobKind(kind)
        const Icon = KIND_ICON[kind] ?? ListChecksIcon
        return (
          <Card key={kind} className="h-full">
            <CardContent className="flex h-full flex-col gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-muted/50 [&_svg]:size-4" aria-hidden="true">
                  <Icon />
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium">{label.label}</p>
                  <p className="truncate text-xs text-muted-foreground" title={KIND_HINT[kind]}>
                    {rate === null ? "还没有结束的任务" : `成功率 ${Math.round(rate * 100)}%`}
                  </p>
                </div>
              </div>
              <div className="flex flex-wrap items-end justify-between gap-3">
                <p className="text-2xl leading-none font-semibold tracking-tight tabular-nums">
                  {formatNumber(total)}
                  <span className="ms-1 text-sm font-normal text-muted-foreground">个</span>
                </p>
                <div className="flex flex-wrap items-center gap-1">
                  {STATUS_TABS.filter((tab) => tab.value !== "all" && (byStatus[tab.value] ?? 0) > 0).map((tab) => {
                    const active = activeKind === kind && activeStatus === tab.value
                    return (
                      <button
                        key={tab.value}
                        type="button"
                        aria-pressed={active}
                        onClick={() => onPick(kind, tab.value)}
                        className={cn(
                          "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs transition-colors outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/30",
                          active ? "border-foreground/15 bg-muted text-foreground" : "text-muted-foreground",
                        )}
                        title={`只看${label.label}中${tab.label}的任务`}
                      >
                        <span className={cn("size-1.5 rounded-full", STATUS_DOT[tab.value])} aria-hidden="true" />
                        {tab.label}
                        <span className="tabular-nums">{formatNumber(byStatus[tab.value] ?? 0)}</span>
                      </button>
                    )
                  })}
                </div>
              </div>
              {rate !== null ? <Progress value={rate * 100} className="h-1" aria-label={`${label.label}成功率`} /> : null}
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
