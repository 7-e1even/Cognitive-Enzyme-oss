import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"
import type { ReactNode } from "react"

import { CopyButton } from "@/components/copy-button"
import { Notice } from "@/components/notice"
import { RelativeTime } from "@/components/relative-time"
import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import type { ConsolidationJob } from "@/lib/api/types"
import { formatDateTime, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { KNOWN_RESULT_KEYS } from "../lib/consolidation"
import { ResultFigures } from "./result-figures"

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-4 py-1.5">
      <span className="shrink-0 text-sm text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-right text-sm">{children}</span>
    </div>
  )
}

export function JobStatusBadge({ job }: { job: ConsolidationJob }) {
  if (!job.status) return <ToneBadge tone="outline" label="还没有运行过" />
  const label = labelOf.jobStatus(job.status)
  return (
    <ToneBadge tone={label.tone}>
      {job.running ? <Spinner className="size-3" /> : null}
      {label.label}
    </ToneBadge>
  )
}

/** 最近一次巩固任务:状态、完成时间、任务编号;失败时给出错误原文,流水线里没有位置的结果键也放在这里,一个都不丢。 */
export function JobCard({ job, isAdmin }: { job: ConsolidationJob; isAdmin: boolean }) {
  const result = job.last_result
  const failedError = job.status === "failed" && typeof result?.error === "string" ? result.error : null
  const stepErrors = Array.isArray(result?.errors) ? (result.errors as unknown[]).map(String) : []
  const unknown = result ? Object.entries(result).filter(([key]) => !KNOWN_RESULT_KEYS.has(key)).map(([key, value]) => ({ key, label: key, value })) : []

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle>最近一次巩固</CardTitle>
          <CardDescription>
            {job.running ? (job.status === "pending" ? "排队中,马上开始" : "正在运行,完成后结果会自动出现") : job.last_run ? "结果已写进左侧各阶段" : "点「立即执行」跑第一次"}
          </CardDescription>
        </div>
        <JobStatusBadge job={job} />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-col divide-y">
          <Row label="完成于">
            {job.last_run ? (
              <>
                {formatDateTime(job.last_run)}
                <span className="text-muted-foreground">
                  {" "}
                  · <RelativeTime value={job.last_run} />
                </span>
              </>
            ) : (
              <span className="text-muted-foreground">{job.running ? "进行中" : "—"}</span>
            )}
          </Row>
          <Row label="任务编号">
            {job.job_id ? (
              <span className="inline-flex items-center gap-1">
                <span className="font-mono text-xs" title={job.job_id}>
                  {shortId(job.job_id, 12)}
                </span>
                <CopyButton value={job.job_id} label="任务编号" />
              </span>
            ) : (
              <span className="text-muted-foreground">—</span>
            )}
          </Row>
          {typeof result?.total_enzymes === "number" ? (
            <Row label="记忆总数">
              <span className="font-medium tabular-nums">{result.total_enzymes}</span>
            </Row>
          ) : null}
        </div>

        {failedError ? (
          <Notice tone="destructive" title="巩固失败">
            <span className="font-mono text-xs break-all">{failedError}</span>
          </Notice>
        ) : null}
        {stepErrors.length ? (
          <Notice tone="warning" title={`${stepErrors.length} 个步骤出错,其余照常完成`}>
            <ul className="flex flex-col gap-1 font-mono text-xs break-all">
              {stepErrors.map((error, index) => (
                <li key={index}>{error}</li>
              ))}
            </ul>
          </Notice>
        ) : null}
        {unknown.length ? (
          <div className="flex flex-col gap-1.5">
            <span className="text-xs font-medium text-muted-foreground">其他结果</span>
            <ResultFigures figures={unknown} />
          </div>
        ) : null}
      </CardContent>
      {isAdmin ? (
        <CardFooter className="justify-end">
          <Button size="xs" variant="ghost" asChild>
            <Link href="/admin/jobs">
              后台任务队列
              <ArrowRightIcon />
            </Link>
          </Button>
        </CardFooter>
      ) : null}
    </Card>
  )
}
