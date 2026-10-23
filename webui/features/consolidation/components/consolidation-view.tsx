"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PlayIcon, SaveIcon, Undo2Icon } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { ErrorState } from "@/components/states"
import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError, errorMessage } from "@/lib/api/client"
import { consolidationApi } from "@/lib/api/endpoints"
import { qk, useHealth, useIsAdmin } from "@/lib/api/hooks"
import type { Consolidation, Schedule } from "@/lib/api/types"
import { formatInterval, formatNumber, formatRelative } from "@/lib/format"

import { RESULT_LABELS, schedulePatch, toDraft, validateDraft, type ScheduleDraft } from "../lib/consolidation"
import { JobCard, JobStatusBadge } from "./job-card"
import { Pipeline } from "./pipeline"
import { ScheduleCard } from "./schedule-card"

/** 巩固跑完后会变的数据:记忆、进化事件、总览、系统状态、任务队列 */
const AFFECTED = [qk.enzymes(), qk.facets, qk.evolution(), qk.dashboard, qk.system, qk.jobs()]

/** 结果里不为零的那几项,拼成一句话放进完成提示 */
function describeResult(result: Record<string, unknown> | null) {
  if (!result) return undefined
  const moved = Object.entries(result)
    .filter(([key, value]) => typeof value === "number" && value > 0 && key !== "total_enzymes" && key !== "rules_enzymes")
    .map(([key, value]) => `${RESULT_LABELS[key]?.label ?? key} ${formatNumber(value as number)}`)
  return moved.length ? moved.join(" · ") : "这一轮没有需要调整的记忆"
}

function describePatch(patch: Partial<Schedule>) {
  const parts: string[] = []
  if (patch.enabled !== undefined) parts.push(patch.enabled ? "开启自动巩固" : "关闭自动巩固")
  if (patch.interval !== undefined) parts.push(`间隔 ${formatInterval(patch.interval)}`)
  if (patch.mutation_enabled !== undefined) parts.push(`${patch.mutation_enabled ? "开启" : "关闭"}突变`)
  if (patch.crossover_enabled !== undefined) parts.push(`${patch.crossover_enabled ? "开启" : "关闭"}交叉`)
  if (patch.dreamglia_enabled !== undefined) parts.push(`${patch.dreamglia_enabled ? "开启" : "关闭"} DreamGlia`)
  if (patch.dreamglia_max_dreams !== undefined) parts.push(`梦境数 ${patch.dreamglia_max_dreams}`)
  if (patch.replay_enabled !== undefined) parts.push(`${patch.replay_enabled ? "开启" : "关闭"}情景回放`)
  if (patch.replay_max_episodes !== undefined) parts.push(`回放情景数 ${patch.replay_max_episodes}`)
  return parts.join(" · ")
}

/**
 * 巩固调度:左边是流水线(每个阶段讲清楚做什么、开关就在旁边、上次的数字在下面),
 * 右边是自动调度与最近一次任务。设置改动先留在草稿里,出现「未保存」与保存 / 放弃;
 * 任务运行中每 2 秒轮询一次,结束时提示结果并刷新受影响的页面数据。
 */
export function ConsolidationView() {
  const queryClient = useQueryClient()
  const isAdmin = useIsAdmin()
  const health = useHealth()
  const query = useQuery({
    queryKey: qk.consolidation,
    queryFn: consolidationApi.get,
    refetchInterval: (current) => (current.state.data?.job.running ? 2000 : 30_000),
  })
  const data = query.data

  const [draft, setDraft] = useState<ScheduleDraft | null>(null)
  const current = draft ?? (data ? toDraft(data.schedule) : null)
  const patch = draft && data ? schedulePatch(draft, data.schedule) : {}
  const dirty = Object.keys(patch).length > 0
  const validation = draft ? validateDraft(draft) : null

  const change = <K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) => {
    if (!data) return
    setDraft((previous) => ({ ...(previous ?? toDraft(data.schedule)), [key]: value }))
  }

  const save = useMutation({
    mutationFn: (next: Partial<Schedule>) => consolidationApi.updateSchedule(next),
    onSuccess: (next, sent) => {
      queryClient.setQueryData<Consolidation>(qk.consolidation, next)
      void queryClient.invalidateQueries({ queryKey: qk.audit() })
      setDraft(null)
      toast.success("巩固调度已保存", { description: describePatch(sent) })
    },
    onError: (error) => toast.error("保存失败", { description: errorMessage(error) }),
  })

  const run = useMutation({
    mutationFn: consolidationApi.run,
    onSuccess: (next) => {
      queryClient.setQueryData<Consolidation>(qk.consolidation, next)
      void queryClient.invalidateQueries({ queryKey: qk.jobs() })
      toast.success("已排队一次巩固", { description: "通常几秒到几分钟完成,结果会自动出现在这一页。" })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast.info("巩固正在运行", { description: "上一次还没结束,等它完成再执行。" })
        void query.refetch()
      } else {
        toast.error(errorMessage(error))
      }
    },
  })

  // 盯住正在跑的那一个任务:它结束时提示一次结果,并刷新受巩固影响的数据
  const watching = useRef<string | null>(null)
  const job = data?.job
  useEffect(() => {
    if (!job?.job_id) return
    if (job.running) {
      watching.current = job.job_id
      return
    }
    if (watching.current !== job.job_id) return
    watching.current = null
    if (job.status === "failed") {
      toast.error("巩固失败", { description: String(job.last_result?.error ?? "查看右侧的错误信息") })
    } else {
      toast.success("巩固完成", { description: describeResult(job.last_result) })
    }
    for (const key of AFFECTED) void queryClient.invalidateQueries({ queryKey: key })
  }, [job?.job_id, job?.running, job?.status, job?.last_result, queryClient])

  const running = Boolean(job?.running)
  const totalEnzymes = job?.last_result?.total_enzymes

  const runButton = (
    <Button size="sm" type="button" variant={dirty ? "outline" : "default"} disabled={!data || running || run.isPending || dirty} onClick={() => run.mutate()}>
      {running || run.isPending ? <Spinner /> : <PlayIcon />}
      {running ? "正在运行" : "立即执行"}
    </Button>
  )

  if (query.isError && !data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />

  return (
    <div className="@container/main flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          {data ? (
            <>
              <span>{data.schedule.enabled ? `每 ${formatInterval(data.schedule.interval)} 自动巩固` : "自动巩固已关闭"}</span>
              <span>{data.job.last_run ? `上次完成于 ${formatRelative(data.job.last_run)}` : data.job.running ? "第一次巩固进行中" : "还没有运行过"}</span>
              {typeof totalEnzymes === "number" ? <span>记忆 {formatNumber(totalEnzymes)} 条</span> : null}
            </>
          ) : (
            <Skeleton className="h-5 w-64" />
          )}
          {dirty ? <ToneBadge tone="warning-light" label="未保存" /> : job && (job.running || job.status === "failed") ? <JobStatusBadge job={job} /> : null}
        </p>
        <div className="flex items-center gap-2">
          {dirty ? (
            <>
              <Button size="sm" type="button" variant="ghost" disabled={save.isPending} onClick={() => setDraft(null)}>
                <Undo2Icon /> 放弃修改
              </Button>
              <Button size="sm" type="button" disabled={Boolean(validation) || save.isPending} onClick={() => save.mutate(patch)}>
                {save.isPending ? <Spinner /> : <SaveIcon />} 保存设置
              </Button>
            </>
          ) : null}
          {dirty ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex">{runButton}</span>
              </TooltipTrigger>
              <TooltipContent>先保存或放弃修改,再立即执行</TooltipContent>
            </Tooltip>
          ) : (
            runButton
          )}
        </div>
      </div>

      {current && data ? (
        <div className="grid grid-cols-1 items-start gap-4 @5xl/main:grid-cols-[minmax(0,1fr)_22rem]">
          <Pipeline draft={current} onChange={change} result={data.job.status === "failed" ? null : data.job.last_result} llmReady={health.data?.llm_configured} disabled={save.isPending} />
          <div className="flex flex-col gap-4 @5xl/main:sticky @5xl/main:top-4">
            <ScheduleCard draft={current} onChange={change} error={validation} disabled={save.isPending} />
            <JobCard job={data.job} isAdmin={isAdmin} />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 @5xl/main:grid-cols-[minmax(0,1fr)_22rem]">
          <Skeleton className="h-[36rem]" />
          <div className="flex flex-col gap-4">
            <Skeleton className="h-64" />
            <Skeleton className="h-48" />
          </div>
        </div>
      )}
    </div>
  )
}
