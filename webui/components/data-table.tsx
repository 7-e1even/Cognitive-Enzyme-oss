"use client"

import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table"
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react"
import { useState } from "react"

import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"

interface DataTableProps<T> {
  columns: ColumnDef<T, unknown>[]
  data: T[]
  loading?: boolean
  empty?: React.ReactNode
  onRowClick?: (row: T) => void
  /** Server-side pagination: the caller owns page/pageSize/total. */
  page?: number
  pageSize?: number
  total?: number
  onPageChange?: (page: number) => void
}

export function DataTable<T>({
  columns,
  data,
  loading,
  empty,
  onRowClick,
  page,
  pageSize,
  total,
  onPageChange,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })
  const paged = page !== undefined && pageSize !== undefined && total !== undefined && onPageChange
  const pages = paged ? Math.max(1, Math.ceil(total! / pageSize!)) : 1

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((group) => (
              <TableRow key={group.id}>
                {group.headers.map((header) => {
                  const sortable = header.column.getCanSort()
                  const dir = header.column.getIsSorted()
                  return (
                    <TableHead key={header.id}>
                      {header.isPlaceholder ? null : sortable ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="-ml-2 h-7 gap-1 px-2"
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {dir === "asc" ? (
                            <ArrowUp className="size-3" />
                          ) : dir === "desc" ? (
                            <ArrowDown className="size-3" />
                          ) : (
                            <ChevronsUpDown className="size-3 opacity-50" />
                          )}
                        </Button>
                      ) : (
                        flexRender(header.column.columnDef.header, header.getContext())
                      )}
                    </TableHead>
                  )
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {loading ? (
              Array.from({ length: 6 }).map((_, i) => (
                <TableRow key={i}>
                  {columns.map((_, j) => (
                    <TableCell key={j}>
                      <Skeleton className="h-4 w-full" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : table.getRowModel().rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={columns.length} className="h-24 text-center text-muted-foreground">
                  {empty ?? "No data"}
                </TableCell>
              </TableRow>
            ) : (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  className={cn(onRowClick && "cursor-pointer")}
                  onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                  ))}
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
      {paged ? (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span className="tabular-nums">
            {total} total · page {page! + 1} / {pages}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page! <= 0} onClick={() => onPageChange!(page! - 1)}>
              Previous
            </Button>
            <Button variant="outline" size="sm" disabled={page! + 1 >= pages} onClick={() => onPageChange!(page! + 1)}>
              Next
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
