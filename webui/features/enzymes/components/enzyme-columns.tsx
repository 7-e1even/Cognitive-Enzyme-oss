"use client"

import type { ColumnDef } from "@tanstack/react-table"

import { RelativeTime } from "@/components/relative-time"
import { Checkbox } from "@/components/ui/checkbox"
import type { EnzymeRow } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"

import { splitReadablePatterns } from "../lib/patterns"
import { ConfidenceMeter, DomainLabel, PatternChips, SeverityBadge, StatusBadge } from "./enzyme-badges"

export function createEnzymeColumns({
  selected,
  onToggle,
  onToggleAll,
}: {
  selected: Set<string>
  onToggle: (id: string) => void
  onToggleAll: (ids: string[], checked: boolean) => void
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
      header: "记忆",
      enableSorting: false,
      cell: ({ row }) => {
        const enzyme = row.original
        return (
          <div className="flex min-w-0 max-w-xl flex-col gap-1">
            <span className="line-clamp-2 text-sm font-medium break-words" title={enzyme.because ? `${enzyme.then}\n\n原因:${enzyme.because}` : enzyme.then}>
              {enzyme.then}
            </span>
            <PatternChips patterns={splitReadablePatterns(enzyme.patterns)} />
          </div>
        )
      },
    },
    {
      id: "domain",
      header: "领域",
      enableSorting: false,
      cell: ({ row }) => <DomainLabel domain={row.original.domain} className="block max-w-32 text-sm" />,
    },
    {
      id: "severity",
      header: "严重度",
      enableSorting: false,
      cell: ({ row }) => <SeverityBadge severity={row.original.severity} />,
    },
    {
      id: "status",
      header: "状态",
      enableSorting: false,
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      id: "confidence",
      header: "置信度",
      enableSorting: false,
      cell: ({ row }) => <ConfidenceMeter value={row.original.confidence} />,
    },
    {
      id: "triggers",
      header: "触发",
      enableSorting: false,
      cell: ({ row }) => <span className="text-sm tabular-nums">{formatNumber(row.original.trigger_count)}</span>,
    },
    {
      id: "updated",
      header: "更新",
      enableSorting: false,
      cell: ({ row }) => <RelativeTime value={row.original.updated_at} className="text-sm text-muted-foreground" />,
    },
  ]
}
