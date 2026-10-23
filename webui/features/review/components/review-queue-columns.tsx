"use client"

import type { ColumnDef } from "@tanstack/react-table"
import { CheckIcon, XIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { EnzymeRow } from "@/lib/api/types"
import { formatDateTime } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { ToneBadge } from "@/components/tone-badge"
import { Skeleton } from "@/components/ui/skeleton"

import { ConfidenceMeter, DomainLabel, QualityScore } from "@/features/enzymes/components/enzyme-badges"

import type { QueueReason } from "../lib/review-data"
import { waitingFor } from "../lib/review-data"

export type ReviewAction = "approve" | "reject"

export function createReviewColumns({
  selected,
  onToggle,
  onToggleAll,
  onAction,
  reasons,
  reasonsLoading,
  busy,
  now,
}: {
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: (ids: string[], checked: boolean) => void
  onAction: (action: ReviewAction, enzyme: EnzymeRow) => void
  reasons: Map<string, QueueReason>
  reasonsLoading: boolean
  busy: boolean
  now: number
}): ColumnDef<EnzymeRow, unknown>[] {
  return [
    {
      id: "select",
      enableSorting: false,
      header: ({ table }) => {
        const ids = table.getRowModel().rows.map((row) => row.original.id)
        const count = ids.filter((id) => selected.has(id)).length
        return (
          <Checkbox
            aria-label="选择本页全部"
            checked={count === 0 ? false : count === ids.length ? true : "indeterminate"}
            onCheckedChange={(checked) => onToggleAll(ids, checked === true)}
          />
        )
      },
      cell: ({ row }) => (
        <div onClick={(event) => event.stopPropagation()}>
          <Checkbox aria-label="选择这条记忆" checked={selected.has(row.original.id)} onCheckedChange={() => onToggle(row.original.id)} />
        </div>
      ),
    },
    {
      id: "memory",
      header: "待审记忆",
      enableSorting: false,
      cell: ({ row }) => {
        const enzyme = row.original
        return (
          <div className="min-w-0 max-w-md">
            <span className="line-clamp-2 text-sm font-medium break-words" title={enzyme.because ? `${enzyme.then}\n\n原因:${enzyme.because}` : enzyme.then}>
              {enzyme.then}
            </span>
          </div>
        )
      },
    },
    {
      id: "reason",
      header: "隔离原因",
      enableSorting: false,
      cell: ({ row }) => {
        const reason = reasons.get(row.original.id)
        if (reason) {
          return (
            <span className="block max-w-48 truncate text-sm" title={reason.full}>
              {reason.short}
            </span>
          )
        }
        return reasonsLoading ? <Skeleton className="h-4 w-20" /> : <span className="text-sm text-muted-foreground">手动送审</span>
      },
    },
    {
      id: "domain",
      header: "领域",
      enableSorting: false,
      cell: ({ row }) => <DomainLabel domain={row.original.domain} className="block max-w-28 text-sm" />,
    },
    {
      id: "origin",
      header: "来源",
      enableSorting: false,
      cell: ({ row }) => {
        const meta = labelOf.origin(row.original.origin)
        return <ToneBadge tone={meta.tone} label={meta.label} />
      },
    },
    {
      id: "quality",
      header: "质量分",
      enableSorting: false,
      cell: ({ row }) => <QualityScore value={row.original.quality_score} />,
    },
    {
      id: "confidence",
      header: "置信度",
      enableSorting: false,
      cell: ({ row }) => <ConfidenceMeter value={row.original.confidence} />,
    },
    {
      id: "waiting",
      header: "已等待",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-sm text-muted-foreground tabular-nums" title={formatDateTime(row.original.created_at)}>
          {waitingFor(row.original.created_at, now)}
        </span>
      ),
    },
    {
      id: "actions",
      header: "",
      enableSorting: false,
      cell: ({ row }) => {
        const enzyme = row.original
        return (
          <div className="flex items-center justify-end gap-1" onClick={(event) => event.stopPropagation()}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button type="button" size="icon-sm" variant="ghost" aria-label={`通过:${enzyme.then}`} disabled={busy} onClick={() => onAction("approve", enzyme)}>
                  <CheckIcon />
                </Button>
              </TooltipTrigger>
              <TooltipContent>通过并启用</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button type="button" size="icon-sm" variant="ghost" aria-label={`拒绝:${enzyme.then}`} disabled={busy} onClick={() => onAction("reject", enzyme)}>
                  <XIcon />
                </Button>
              </TooltipTrigger>
              <TooltipContent>拒绝</TooltipContent>
            </Tooltip>
          </div>
        )
      },
    },
  ]
}
