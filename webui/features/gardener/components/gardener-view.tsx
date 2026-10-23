"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FileWarningIcon, PlayIcon, Settings2Icon, SquareIcon } from "lucide-react"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError, errorMessage } from "@/lib/api/client"
import { gardenerApi } from "@/lib/api/endpoints"
import { qk, useIsAdmin } from "@/lib/api/hooks"
import type { GardenerLog, GardenerState, GardenerStatus } from "@/lib/api/types"

import { textToLines, useGardenerLog } from "../hooks/use-gardener-log"
import { HOURS, MODE_LABEL, OUTCOME_LABEL, outcomeOf } from "../lib/gardener"
import { ChecksCard, CommandCard, OutcomeBadge, RunCard } from "./gardener-cards"
import { GardenerSettingsSheet } from "./gardener-settings-sheet"
import { LogViewer } from "./log-viewer"

/** 园丁回写的反馈与新假设会改动这些数据 */
const AFFECTED = [qk.enzymes(), qk.facets, qk.evolution(), qk.dashboard]
const checkKey = [...qk.gardener, "check"] as const

function statusOf(log: GardenerLog): GardenerStatus {
  return { running: log.running, started_at: log.started_at, finished_at: log.finished_at, exit_code: log.exit_code, error: log.error, log_lines: log.log_lines }
}

/** 第一行日志是园丁自己写的「[gardener] 启动(manual): …」,触发方式从这里读 */
function triggerFrom(firstLine: string | undefined) {
  return firstLine?.match(/^\[gardener\] 启动\((\w+)\)/)?.[1] ?? null
}

function lastSegment(path: string) {
  const parts = path.split(/[\\/]+/).filter(Boolean)
  return parts[parts.length - 1] ?? path
}

/**
 * 夜间园丁:headless Claude Code 对照真实代码库复核记忆,带证据回写反馈或提交新假设。
 * 左边是实时日志,右边是本次 / 上次运行、环境自检和外部调度命令。配置收在设置抽屉里(只给管理员);操作员可以运行与停止。
 */
export function GardenerView() {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const isAdmin = useIsAdmin()
  const [settingsOpen, setSettingsOpen] = useState(false)

  const query = useQuery({
    queryKey: qk.gardener,
    queryFn: gardenerApi.get,
    refetchInterval: (current) => (current.state.data?.status.running ? 5000 : 30_000),
  })
  const checks = useQuery({ queryKey: checkKey, queryFn: gardenerApi.check, staleTime: 60_000 })

  const data = query.data
  const running = Boolean(data?.status.running)

  const setStatus = (status: GardenerStatus, lastRun?: GardenerState["last_run"]) =>
    queryClient.setQueryData<GardenerState>(qk.gardener, (previous) => (previous ? { ...previous, status, last_run: lastRun ?? previous.last_run } : previous))

  const log = useGardenerLog({
    running,
    onUpdate: (next) => {
      // 夜间定时在这时启动、或者别的标签页点了运行:日志先知道,页面状态跟上
      if (next.running !== running || next.log_lines !== data?.status.log_lines) setStatus(statusOf(next))
    },
    onFinished: (next) => {
      setStatus(statusOf(next), next.last_run)
      const outcome = outcomeOf({ ...next, stopped: next.last_run?.stopped ?? false, running: false })
      const label = OUTCOME_LABEL[outcome].label
      if (outcome === "succeeded") toast.success(`园丁${label}`, { description: "回写的反馈与新假设已经进入记忆库。" })
      else if (outcome === "stopped") toast.info("园丁已停止", { description: "停止前已经回写的内容会保留。" })
      else toast.error(`园丁${label}`, { description: next.error || "查看日志末尾的输出。" })
      for (const key of AFFECTED) void queryClient.invalidateQueries({ queryKey: key })
    },
  })

  const run = useMutation({
    mutationFn: gardenerApi.run,
    onSuccess: (result) => {
      log.restart()
      setStatus({ running: true, started_at: new Date().toISOString(), finished_at: null, exit_code: null, error: "", log_lines: 0 })
      void query.refetch()
      toast.success(result.message)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) toast.info("园丁已经在运行", { description: "等这一次结束,或者先停止它。" })
      else toast.error("没能启动园丁", { description: errorMessage(error) })
      void query.refetch()
      void checks.refetch()
    },
  })

  const stop = useMutation({
    mutationFn: gardenerApi.stop,
    onSuccess: (result) => toast.info(result.message),
    onError: (error) => toast.error("没能停止园丁", { description: errorMessage(error) }),
  })

  const askStop = async () => {
    const ok = await confirm({
      title: "停止这次运行?",
      description: "会立即终止 Claude Code 进程(docker 模式同时删除容器)。已经回写到记忆库的反馈会保留,记为「已手动停止」。",
      confirmText: "停止运行",
      destructive: true,
    })
    if (ok) stop.mutate()
  }

  // 服务重启后内存里的日志是空的:退回持久化的夜报尾部(最后 200 行)
  const tail = data?.last_run?.log_tail
  const useTail = log.loaded && log.lines.length === 0 && !running && Boolean(tail?.length)
  const tailLines = useMemo(() => (useTail && tail ? textToLines(tail) : []), [useTail, tail])
  const lines = useTail ? tailLines : log.lines
  const trigger = running ? triggerFrom(log.lines[0]?.text) : null

  const config = data?.config
  const failedChecks = checks.data?.checks.filter((check) => !check.ok).length ?? 0

  const runButton = running ? (
    <Button size="sm" variant="destructive" disabled={stop.isPending} onClick={() => void askStop()}>
      {stop.isPending ? <Spinner /> : <SquareIcon />}
      停止运行
    </Button>
  ) : (
    <Button size="sm" disabled={!data || run.isPending || !data.prompt_exists} onClick={() => run.mutate()}>
      {run.isPending ? <Spinner /> : <PlayIcon />}
      立即运行
    </Button>
  )

  if (query.isError && !data) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />

  return (
    <div className="@container/main flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
          {running ? <OutcomeBadge outcome="running" /> : null}
          {config ? (
            <>
              <span>{MODE_LABEL[config.mode].short}</span>
              <span>{config.nightly_enabled ? `每晚 ${HOURS[config.nightly_hour]?.label ?? `${config.nightly_hour}:00`} 自动运行` : "夜间定时未开启"}</span>
              <span>{config.model_id ? `模型 ${config.model_id}` : config.model ? `模型 ${config.model}` : "Claude Code 默认模型"}</span>
              <span>{config.repo_path ? `仓库 ${lastSegment(config.repo_path)}` : "未指定仓库"}</span>
            </>
          ) : (
            <Skeleton className="h-5 w-72" />
          )}
        </p>
        <div className="flex items-center gap-2">
          {isAdmin ? (
            <Button size="sm" variant="outline" disabled={!config} onClick={() => setSettingsOpen(true)}>
              <Settings2Icon /> 设置
            </Button>
          ) : null}
          {runButton}
        </div>
      </div>

      {data && !data.prompt_exists ? (
        <Notice tone="destructive" title="找不到园丁的操作手册">
          <span className="flex items-start gap-1.5">
            <FileWarningIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              提示词文件 <code className="font-mono">ops/gardener/gardener.md</code> 不在后端的工作目录里,园丁无法启动。请确认部署时包含了 <code className="font-mono">ops/</code> 目录。
            </span>
          </span>
        </Notice>
      ) : null}

      {data ? (
        <div className="grid grid-cols-1 items-start gap-4 @5xl/main:grid-cols-[minmax(0,1fr)_22rem]">
          <LogViewer lines={lines} running={running} loaded={log.loaded} source={useTail ? "tail" : "live"} />
          <div className="flex flex-col gap-4 @5xl/main:sticky @5xl/main:top-4">
            <RunCard status={data.status} lastRun={data.last_run} trigger={trigger} />
            <ChecksCard checks={checks.data?.checks} loading={checks.isLoading} fetching={checks.isFetching} onRecheck={() => void checks.refetch()} />
            {isAdmin && data.command ? <CommandCard command={data.command} /> : null}
            {failedChecks > 0 && !running && isAdmin ? <p className="px-1 text-xs text-muted-foreground">自检未通过的项目大多在「设置」里就能改;令牌在「设置 → 访问与安全」生成。</p> : null}
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 @5xl/main:grid-cols-[minmax(0,1fr)_22rem]">
          <Skeleton className="h-[36rem]" />
          <div className="flex flex-col gap-4">
            <Skeleton className="h-64" />
            <Skeleton className="h-72" />
          </div>
        </div>
      )}

      {config && isAdmin ? <GardenerSettingsSheet open={settingsOpen} onOpenChange={setSettingsOpen} config={config} /> : null}
      {confirmDialog}
    </div>
  )
}
