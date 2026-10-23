"use client"

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FilterIcon, ListChecksIcon, RefreshCwIcon } from "lucide-react"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { DataTable } from "@/components/data-table"
import { ErrorState } from "@/components/states"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { copyText } from "@/lib/clipboard"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { Job } from "@/lib/api/types"
import { formatNumber, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { createJobColumns, type JobAction } from "./job-columns"
import { JobKindCards } from "./job-kind-cards"
import { JobSheet } from "./job-sheet"
import { isActive, KNOWN_KINDS, STATUS_DOT, STATUS_TABS, type StatusTab } from "./lib/data"
import { PAGE_SIZE, useJobsParams } from "./lib/use-jobs-params"

const EMPTY_COUNTS: Record<string, Record<string, number>> = {}

function sumStatus(counts: Record<string, Record<string, number>>, kind: string, status: string) {
  return Object.entries(counts)
    .filter(([k]) => !kind || k === kind)
    .reduce((total, [, byStatus]) => total + (status === "all" ? Object.values(byStatus).reduce((a, b) => a + b, 0) : (byStatus[status] ?? 0)), 0)
}

/**
 * 后台任务:有没有任务卡住或失败。服务端分页,筛选与页签在 URL 里;
 * 有任务在排队或运行时每 3 秒自动刷新,都结束了就降到 30 秒。
 */
export function JobsView() {
  const queryClient = useQueryClient()
  const { params, setParams } = useJobsParams()
  const { kind, status, page } = params

  const query = useQuery({
    queryKey: qk.jobs({ kind, status, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }),
    queryFn: () =>
      adminApi.jobs({ kind: kind || undefined, status: status === "all" ? undefined : status, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }),
    placeholderData: keepPreviousData,
    refetchInterval: (q) => {
      const data = q.state.data
      if (!data) return false
      const busy =
        data.items.some((job) => isActive(job.status)) ||
        Object.values(data.counts).some((byStatus) => (byStatus.pending ?? 0) + (byStatus.running ?? 0) > 0)
      return busy ? 3_000 : 30_000
    },
  })

  const counts = query.data?.counts ?? EMPTY_COUNTS
  const items = useMemo(() => query.data?.items ?? [], [query.data])
  const total = query.data?.total ?? 0
  const kinds = useMemo(() => Array.from(new Set([...KNOWN_KINDS, ...Object.keys(counts)])), [counts])
  const busy = sumStatus(counts, "", "pending") + sumStatus(counts, "", "running")
  const failed = sumStatus(counts, "", "failed")
  const everything = sumStatus(counts, "", "all")

  // 详情面板:保存 id,从最新一页里取对象,重试后状态变化能立刻反映;翻页后不在当前页也保留最后一份
  const [sheetOpen, setSheetOpen] = useState(false)
  const [selected, setSelected] = useState<Job | null>(null)
  const liveSelected = (selected && items.find((job) => job.id === selected.id)) ?? selected

  const retry = useMutation({
    mutationFn: (id: string) => adminApi.retryJob(id),
    onSuccess: (_, id) => {
      toast.success("已重新排队", { description: `任务 ${shortId(id)} 会由后台 worker 重新执行` })
      void queryClient.invalidateQueries({ queryKey: qk.jobs() })
      void queryClient.invalidateQueries({ queryKey: qk.dashboard })
    },
  })
  const { mutate: retryJob, reset: resetRetry } = retry

  // Manual useCallback/useMemo removed: React Compiler memoizes these, and keeping both trips its lint rule.
  const openJob = (job: Job) => {
    setSelected(job)
    setSheetOpen(true)
    resetRetry()
  }

  const handleAction = (action: JobAction, job: Job) => {
    if (action === "open") openJob(job)
    else if (action === "copy") void copyText(job.id, "任务 ID")
    else retryJob(job.id)
  }

  // 进行中任务的耗时按「这一页数据取回的时刻」计算,随每次自动刷新前进
  const now = query.dataUpdatedAt
  const columns = createJobColumns({ onAction: handleAction, now })

  const filtered = Boolean(kind) || status !== "all"
  const kindOptions = [{ value: "all", label: "全部类型" }, ...kinds.map((k) => ({ value: k, label: labelOf.jobKind(k).label }))]

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {query.data
            ? `共 ${formatNumber(everything)} 个任务${busy > 0 ? ` · ${formatNumber(busy)} 个进行中,每 3 秒自动刷新` : ""}`
            : "正在读取…"}
          {failed > 0 ? (
            <Badge variant="destructive" className="ms-2 tabular-nums">
              {formatNumber(failed)} 个失败
            </Badge>
          ) : null}
        </p>
        <div className="flex items-center gap-2">
          <Select value={kind || "all"} onValueChange={(value) => setParams({ kind: !value || value === "all" ? "" : value })}>
            <SelectTrigger size="sm" className="w-[150px]" aria-label="按任务类型筛选">
              <FilterIcon className="text-muted-foreground" aria-hidden="true" />
              <SelectValue />
            </SelectTrigger>
            <SelectContent align="end">
              <SelectGroup>
                {kindOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
            <RefreshCwIcon className={cn(query.isFetching && "animate-spin")} aria-hidden="true" /> 刷新
          </Button>
        </div>
      </div>

      <JobKindCards
        counts={counts}
        kinds={kinds}
        loading={!query.data}
        activeKind={kind}
        activeStatus={status}
        onPick={(nextKind, nextStatus) => setParams({ kind: nextKind, status: nextStatus })}
      />

      {query.isError && !query.data ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} title="任务列表加载失败" />
      ) : (
        <div className="flex flex-col gap-3">
          <Tabs value={status} onValueChange={(value) => setParams({ status: value as StatusTab })}>
            <TabsList>
              {STATUS_TABS.map((tab) => {
                const n = sumStatus(counts, kind, tab.value)
                return (
                  <TabsTrigger key={tab.value} value={tab.value} className="gap-1.5">
                    {tab.value !== "all" ? <span className={cn("size-1.5 rounded-full", STATUS_DOT[tab.value])} aria-hidden="true" /> : null}
                    {tab.label}
                    <span className={cn("text-xs tabular-nums", tab.value === "failed" && n > 0 ? "text-destructive" : "text-muted-foreground")}>{formatNumber(n)}</span>
                  </TabsTrigger>
                )
              })}
            </TabsList>
          </Tabs>
          <div className={cn("transition-opacity", query.isPlaceholderData && "opacity-60")}>
            <DataTable
              columns={columns}
              data={items}
              loading={!query.data}
              onRowClick={openJob}
              page={page - 1}
              pageSize={PAGE_SIZE}
              total={total}
              onPageChange={(next) => setParams({ page: next + 1 })}
              empty={
                <span className="flex flex-col items-center gap-2 py-4">
                  <ListChecksIcon className="size-5" aria-hidden="true" />
                  {filtered ? (
                    <>
                      没有符合条件的任务
                      <Button variant="outline" size="sm" onClick={() => setParams({ kind: "", status: "all" })}>
                        清除筛选
                      </Button>
                    </>
                  ) : (
                    "还没有后台任务。会话结束时 hook 会提交会话捕获任务,记忆巩固按调度定期执行。"
                  )}
                </span>
              }
            />
          </div>
        </div>
      )}

      <JobSheet
        job={liveSelected}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onRetry={(job) => retryJob(job.id)}
        retrying={retry.isPending}
        retryError={retry.error}
      />
    </div>
  )
}
