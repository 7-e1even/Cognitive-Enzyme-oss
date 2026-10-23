"use client"

import type { ColumnDef } from "@tanstack/react-table"

import { RelativeTime } from "@/components/relative-time"
import { ToneBadge } from "@/components/tone-badge"
import type { DecisionRow } from "@/lib/api/types"
import { formatNumber, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { judgeLabel } from "../lib/data"

function StateCell({ row }: { row: DecisionRow }) {
  const meta = [row.session_id ? `会话 ${shortId(row.session_id, 12)}` : "", row.project, row.domain].filter(Boolean)
  return (
    <div className="flex min-w-0 max-w-md flex-col gap-0.5">
      <span className="truncate text-sm" title={row.state}>
        {row.state || <span className="text-muted-foreground">(没有情境文本)</span>}
      </span>
      <span className="truncate text-xs text-muted-foreground">{meta.length ? meta.join(" · ") : "—"}</span>
    </div>
  )
}

function CountCell({ value, tone }: { value: number; tone?: "good" | "info" }) {
  return (
    <span
      className={cn(
        "text-sm tabular-nums",
        value === 0 && "text-muted-foreground",
        value > 0 && tone === "good" && "font-medium text-emerald-600 dark:text-emerald-400",
        value > 0 && tone === "info" && "font-medium text-sky-600 dark:text-sky-400",
      )}
    >
      {formatNumber(value)}
    </span>
  )
}

/** 服务端分页、按时间倒序,列头不排序。点一行打开判定详情。 */
export function createDecisionColumns(): ColumnDef<DecisionRow, unknown>[] {
  return [
    {
      id: "time",
      header: "时间",
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex flex-col gap-0.5">
          <RelativeTime value={row.original.created_at} className="text-sm" />
          <span className="font-mono text-xs text-muted-foreground">{shortId(row.original.id)}</span>
        </div>
      ),
    },
    { id: "state", header: "情境", enableSorting: false, cell: ({ row }) => <StateCell row={row.original} /> },
    {
      id: "source",
      header: "来源",
      enableSorting: false,
      cell: ({ row }) => {
        const label = labelOf.decisionSource(row.original.source)
        return <ToneBadge tone={label.tone} label={label.label} />
      },
    },
    {
      id: "judge",
      header: "判官",
      enableSorting: false,
      cell: ({ row }) => {
        const label = judgeLabel(row.original.judge)
        return (
          <div className="flex min-w-0 flex-col items-start gap-0.5">
            <ToneBadge tone={label.tone} label={label.label} title={label.hint} />
            {row.original.model ? <span className="max-w-full truncate font-mono text-xs text-muted-foreground">{row.original.model}</span> : null}
          </div>
        )
      },
    },
    { id: "candidates", header: "候选", enableSorting: false, cell: ({ row }) => <CountCell value={row.original.candidates} /> },
    { id: "injected", header: "注入", enableSorting: false, cell: ({ row }) => <CountCell value={row.original.injected} /> },
    { id: "outcomes", header: "结果", enableSorting: false, cell: ({ row }) => <CountCell value={row.original.outcomes} tone="good" /> },
    { id: "labels", header: "标注", enableSorting: false, cell: ({ row }) => <CountCell value={row.original.labels} tone="info" /> },
  ]
}
