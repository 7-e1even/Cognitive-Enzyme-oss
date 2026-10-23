"use client"

import type { ColumnDef } from "@tanstack/react-table"
import { useMemo } from "react"

import { DataTable } from "@/components/data-table"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { SystemStatus } from "@/lib/api/types"
import { formatBytes, formatNumber, formatPercent } from "@/lib/format"

import { tableRows, type TableRow } from "./lib/system"

const columns: ColumnDef<TableRow, unknown>[] = [
  {
    id: "name",
    accessorFn: (row) => row.name,
    header: "表",
    cell: ({ row }) => (
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="truncate font-mono text-xs">{row.original.name}</span>
        {row.original.label ? <span className="truncate text-xs text-muted-foreground">{row.original.label}</span> : null}
      </div>
    ),
  },
  {
    id: "rows",
    accessorFn: (row) => row.rows,
    header: "行数",
    cell: ({ row }) => <span className="block text-right text-sm tabular-nums">{formatNumber(row.original.rows)}</span>,
  },
  {
    id: "bytes",
    accessorFn: (row) => row.bytes,
    header: "占用",
    cell: ({ row }) => <span className="block text-right text-sm tabular-nums">{formatBytes(row.original.bytes)}</span>,
  },
  {
    id: "share",
    accessorFn: (row) => row.share,
    header: "占比",
    cell: ({ row }) => (
      <div className="flex min-w-0 items-center gap-2">
        <div className="h-1.5 min-w-10 flex-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-primary/70" style={{ width: `${Math.max(1, row.original.share * 100)}%` }} />
        </div>
        <span className="w-11 shrink-0 text-right text-xs tabular-nums text-muted-foreground">{formatPercent(row.original.share)}</span>
      </div>
    ),
  },
]

/** 各表的行数与磁盘占用(含索引),按占用从大到小;列头可以切换排序。小表是精确计数,大表是统计估计。 */
export function TableSizes({ system, loading }: { system: SystemStatus | undefined; loading: boolean }) {
  const rows = useMemo(() => [...tableRows(system?.tables)].sort((a, b) => b.bytes - a.bytes), [system?.tables])
  const totalBytes = rows.reduce((sum, row) => sum + row.bytes, 0)
  const totalRows = rows.reduce((sum, row) => sum + row.rows, 0)
  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle className="text-sm">数据表</CardTitle>
        <CardDescription className="text-xs">
          {rows.length ? `${rows.length} 张表 · ${formatNumber(totalRows)} 行 · 共 ${formatBytes(totalBytes)}` : "PostgreSQL 各表的大小"}
        </CardDescription>
      </CardHeader>
      <div className="p-3">
        <DataTable columns={columns} data={rows} loading={loading} empty="还没有数据表信息" />
      </div>
    </Card>
  )
}
