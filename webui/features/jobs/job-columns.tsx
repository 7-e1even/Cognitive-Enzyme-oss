"use client"

import type { ColumnDef } from "@tanstack/react-table"
import { CopyIcon, EllipsisVerticalIcon, EyeIcon, RotateCcwIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import type { Job } from "@/lib/api/types"
import { formatRelative, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { JobStatusBadge } from "./job-status-badge"
import { jobSeconds, retryState, shortDuration, triggerLabel } from "./lib/data"

export type JobAction = "open" | "retry" | "copy"

/** 服务端分页、按创建时间倒序,所以列头不提供排序——只排当前一页会让人误以为排的是全部。整行点击打开详情。 */
export function createJobColumns({
  onAction,
  now,
}: {
  onAction: (action: JobAction, job: Job) => void
  now: number
}): ColumnDef<Job, unknown>[] {
  return [
    {
      id: "task",
      header: "任务",
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-sm font-medium">{labelOf.jobKind(row.original.kind).label}</span>
          <span className="truncate font-mono text-xs text-muted-foreground">{shortId(row.original.id, 12)}</span>
        </div>
      ),
    },
    {
      id: "status",
      header: "状态",
      enableSorting: false,
      cell: ({ row }) => <JobStatusBadge status={row.original.status} />,
    },
    {
      id: "session",
      header: "会话 / 项目",
      enableSorting: false,
      cell: ({ row }) => {
        const job = row.original
        if (!job.session_id && !job.project) return <span className="text-sm text-muted-foreground">—</span>
        return (
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="truncate font-mono text-xs" title={job.session_id ?? undefined}>
              {job.session_id ? shortId(job.session_id, 14) : "—"}
            </span>
            {job.project ? <span className="truncate text-xs text-muted-foreground">{job.project}</span> : null}
          </div>
        )
      },
    },
    {
      id: "trigger",
      header: "触发",
      enableSorting: false,
      cell: ({ row }) => (
        <span className="text-sm text-muted-foreground">
          {row.original.kind === "capture" && !row.original.trigger ? "会话结束" : triggerLabel(row.original.trigger)}
        </span>
      ),
    },
    {
      id: "created",
      header: "创建",
      enableSorting: false,
      cell: ({ row }) => <span className="text-sm text-muted-foreground">{formatRelative(row.original.created_at)}</span>,
    },
    {
      id: "duration",
      header: "耗时",
      enableSorting: false,
      cell: ({ row }) => <span className="text-sm tabular-nums text-muted-foreground">{shortDuration(jobSeconds(row.original, now))}</span>,
    },
    {
      id: "actions",
      header: () => <span className="sr-only">操作</span>,
      enableSorting: false,
      cell: ({ row }) => {
        const job = row.original
        const retry = retryState(job)
        return (
          <div className="flex justify-end" onClick={(event) => event.stopPropagation()}>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button type="button" size="icon-sm" variant="ghost" aria-label="任务操作">
                  <EllipsisVerticalIcon aria-hidden="true" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-44">
                <DropdownMenuItem onClick={() => onAction("open", job)}>
                  <EyeIcon aria-hidden="true" /> 查看详情
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => onAction("copy", job)}>
                  <CopyIcon aria-hidden="true" /> 复制任务 ID
                </DropdownMenuItem>
                {job.status === "failed" ? (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem disabled={!retry.ok} title={retry.reason} onClick={() => onAction("retry", job)}>
                      <RotateCcwIcon aria-hidden="true" /> 重试任务
                    </DropdownMenuItem>
                  </>
                ) : null}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        )
      },
    },
  ]
}
