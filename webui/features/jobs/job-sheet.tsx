"use client"

import { InfoIcon, RotateCcwIcon } from "lucide-react"
import type { ReactNode } from "react"

import { CopyButton } from "@/components/copy-button"
import { JsonView } from "@/components/json-view"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError, errorMessage } from "@/lib/api/client"
import type { Job } from "@/lib/api/types"
import { formatDateTime, formatNumber, formatRelative, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { JobStatusBadge } from "./job-status-badge"
import { jobSeconds, KIND_HINT, RESULT_LABEL, retryState, shortDuration, STATUS_DOT, triggerLabel } from "./lib/data"

const TABS = [
  { value: "summary", label: "概况" },
  { value: "result", label: "结果" },
  { value: "raw", label: "原始数据" },
] as const

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm break-words">{children}</dd>
    </div>
  )
}

function ResultView({ job }: { job: Job }) {
  const entries = Object.entries(job.result ?? {})
  const errors = job.result?.errors
  const stepErrors = Array.isArray(errors) ? (errors as unknown[]).map(String) : []
  const scalars = entries.filter(([key, value]) => key !== "errors" && (typeof value === "number" || typeof value === "string" || typeof value === "boolean"))
  const nested = entries.filter(([key, value]) => key !== "errors" && value !== null && typeof value === "object")
  const errorCount = typeof errors === "number" ? errors : null

  if (entries.length === 0) {
    return <p className="py-10 text-center text-sm text-muted-foreground">{job.status === "succeeded" ? "任务没有写回统计。" : "任务还没有结果。"}</p>
  }
  return (
    <div className="flex flex-col gap-5">
      {stepErrors.length > 0 ? (
        <Notice tone="warning" title={`部分步骤出错(${stepErrors.length})`}>
          <ul className="flex flex-col gap-0.5 font-mono text-xs">
            {stepErrors.map((error, index) => (
              <li key={index} className="break-all">
                {error}
              </li>
            ))}
          </ul>
        </Notice>
      ) : null}
      {scalars.length > 0 || errorCount !== null ? (
        <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {scalars.map(([key, value]) => (
            <div key={key} className="flex min-w-0 flex-col gap-1 rounded-lg border bg-muted/40 px-3 py-2">
              <dt className="truncate text-xs text-muted-foreground" title={key}>
                {RESULT_LABEL[key] ?? key}
              </dt>
              <dd className="truncate text-base font-semibold tabular-nums">{typeof value === "number" ? formatNumber(value) : String(value)}</dd>
            </div>
          ))}
          {errorCount !== null ? (
            <div className={cn("flex min-w-0 flex-col gap-1 rounded-lg border px-3 py-2", errorCount > 0 ? "border-destructive/30 bg-destructive/5" : "bg-muted/40")}>
              <dt className="truncate text-xs text-muted-foreground">{RESULT_LABEL.errors}</dt>
              <dd className={cn("text-base font-semibold tabular-nums", errorCount > 0 && "text-destructive")}>{formatNumber(errorCount)}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {nested.map(([key, value]) => (
        <div key={key} className="flex flex-col gap-2">
          <h4 className="text-xs font-medium text-muted-foreground">{RESULT_LABEL[key] ?? key}</h4>
          <JsonView value={value} />
        </div>
      ))}
    </div>
  )
}

/** 任务详情抽屉:概况(失败时先给错误)、结果(worker 写回的统计)、原始数据;失败的任务可以重试。 */
export function JobSheet({
  job,
  open,
  onOpenChange,
  onRetry,
  retrying,
  retryError,
}: {
  job: Job | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onRetry: (job: Job) => void
  retrying: boolean
  /** 这条任务最近一次重试的错误(409 等),就地显示 */
  retryError: unknown
}) {
  const retry = job ? retryState(job) : { ok: false }
  const kind = job ? labelOf.jobKind(job.kind) : null
  const conflict = retryError instanceof ApiError && retryError.status === 409

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex flex-col gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg">
        <SheetHeader className="shrink-0 gap-0 border-b p-0">
          <SheetTitle className="sr-only">后台任务</SheetTitle>
          <SheetDescription className="sr-only">查看后台任务的错误、结果统计与原始数据;失败的任务可以重试。</SheetDescription>
          {job && kind ? (
            <div className="flex min-w-0 flex-col gap-2.5 px-5 py-4 pe-12">
              <div className="flex min-w-0 items-center gap-2">
                <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", STATUS_DOT[job.status])} />
                <h2 className="min-w-0 flex-1 truncate text-lg font-semibold tracking-tight">{kind.label}</h2>
                <JobStatusBadge status={job.status} />
              </div>
              <p className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
                <span className="font-mono">{shortId(job.id, 16)}</span>
                <CopyButton value={job.id} label="任务 ID" />
                <span>·</span>
                <span>创建于 {formatRelative(job.created_at)}</span>
                <span>·</span>
                <span>第 {job.attempts} 次尝试</span>
              </p>
            </div>
          ) : null}
        </SheetHeader>

        {job ? (
          <Tabs key={job.id} defaultValue={job.status === "succeeded" ? "result" : "summary"} className="flex min-h-0 flex-1 flex-col gap-0">
            <div className="shrink-0 border-b px-5 py-2">
              <TabsList>
                {TABS.map((tab) => (
                  <TabsTrigger key={tab.value} value={tab.value}>
                    {tab.label}
                  </TabsTrigger>
                ))}
              </TabsList>
            </div>
            <ScrollArea className="min-h-0 flex-1">
              <TabsContent value="summary" className="mt-0">
                <div className="flex flex-col gap-5 px-5 py-5">
                  {job.status === "failed" ? (
                    <Notice tone="destructive" title="任务失败">
                      <span className="font-mono text-xs break-all whitespace-pre-wrap">{job.error || "没有记录错误信息"}</span>
                    </Notice>
                  ) : job.status === "succeeded" ? (
                    <Notice tone="success" title="任务已完成">
                      {job.finished_at ? `${formatDateTime(job.finished_at)} 完成,` : ""}用时 {shortDuration(jobSeconds(job))}(含排队)。
                    </Notice>
                  ) : (
                    <Notice tone="info" title={job.status === "running" ? "正在执行" : "排队等待中"}>
                      {job.error
                        ? job.error
                        : job.status === "running"
                          ? "后台 worker 正在处理,完成后这里会自动更新。"
                          : "同类任务一次只跑一个,前面的完成后就会轮到它。"}
                    </Notice>
                  )}
                  {job.status === "failed" && !retry.ok && retry.reason ? (
                    <p className="flex gap-2 text-xs leading-relaxed text-muted-foreground">
                      <InfoIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                      {retry.reason}
                    </p>
                  ) : null}
                  {conflict ? (
                    <Notice tone="warning" title="重试被拒绝">
                      {errorMessage(retryError)}
                    </Notice>
                  ) : null}
                  <dl className="grid grid-cols-[6.5rem_1fr] gap-x-4 gap-y-3">
                    <Fact label="类型">
                      <span title={KIND_HINT[job.kind]}>{kind?.label}</span>
                    </Fact>
                    <Fact label="触发">{job.kind === "capture" && !job.trigger ? "会话结束(hook)" : triggerLabel(job.trigger)}</Fact>
                    <Fact label="会话">{job.session_id ? <span className="font-mono text-xs">{job.session_id}</span> : "—"}</Fact>
                    <Fact label="项目">{job.project || "—"}</Fact>
                    <Fact label="对话原文">
                      {job.transcript_chars ? `${formatNumber(job.transcript_chars)} 字` : job.kind === "capture" ? "已清除(处理完毕后不保留原文)" : "—"}
                    </Fact>
                    <Fact label="尝试次数">{job.attempts}</Fact>
                    <Fact label="创建时间">{formatDateTime(job.created_at)}</Fact>
                    <Fact label="最后更新">{formatDateTime(job.updated_at)}</Fact>
                    <Fact label="完成时间">{formatDateTime(job.finished_at)}</Fact>
                    <Fact label="耗时">{shortDuration(jobSeconds(job))}</Fact>
                  </dl>
                </div>
              </TabsContent>
              <TabsContent value="result" className="mt-0">
                <div className="px-5 py-5">
                  <ResultView job={job} />
                </div>
              </TabsContent>
              <TabsContent value="raw" className="mt-0">
                <div className="px-5 py-5">
                  <JsonView value={job} />
                </div>
              </TabsContent>
            </ScrollArea>
          </Tabs>
        ) : null}

        <SheetFooter className="shrink-0 flex-row flex-wrap items-center justify-between gap-2 border-t bg-muted/40 px-5 py-3">
          <SheetClose asChild>
            <Button type="button" variant="ghost" size="sm">
              关闭
            </Button>
          </SheetClose>
          {job?.status === "failed" ? (
            retry.ok ? (
              <Button type="button" size="sm" disabled={retrying} onClick={() => onRetry(job)}>
                {retrying ? <Spinner /> : <RotateCcwIcon aria-hidden="true" />}
                重试任务
              </Button>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span tabIndex={0} className="inline-flex">
                    <Button type="button" size="sm" disabled>
                      <RotateCcwIcon aria-hidden="true" />
                      重试任务
                    </Button>
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-64">{retry.reason}</TooltipContent>
              </Tooltip>
            )
          ) : null}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
