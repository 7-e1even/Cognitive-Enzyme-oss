"use client"

import { CircleCheckIcon, CircleXIcon, RefreshCwIcon } from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"

import { CodeSnippet } from "@/components/code-snippet"
import { Hint } from "@/components/hint"
import { Notice } from "@/components/notice"
import { RelativeTime } from "@/components/relative-time"
import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { GardenerCheck, GardenerLastRun, GardenerStatus } from "@/lib/api/types"
import { formatDateTime, formatDuration } from "@/lib/format"
import { cn } from "@/lib/utils"

import { durationSeconds, OUTCOME_LABEL, outcomeOf, TRIGGER_LABEL, type RunOutcome } from "../lib/gardener"

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-4 py-1.5">
      <span className="shrink-0 text-sm text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-right text-sm tabular-nums">{children}</span>
    </div>
  )
}

export function OutcomeBadge({ outcome }: { outcome: RunOutcome }) {
  const meta = OUTCOME_LABEL[outcome]
  return (
    <ToneBadge tone={meta.tone}>
      {outcome === "running" ? <Spinner className="size-3" /> : null}
      {meta.label}
    </ToneBadge>
  )
}

/** 运行中每秒走一次的时钟;不在运行时不开计时器 */
function useTicker(active: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

/**
 * 运行情况:运行中显示这一次的实时状态;空闲时显示最近一次运行的结局。
 * 结局区分「完成 / 失败 / 已手动停止 / 超时终止」——手动停止不该被当成失败报出来。
 */
export function RunCard({ status, lastRun, trigger }: { status: GardenerStatus; lastRun: GardenerLastRun | null; trigger: string | null }) {
  const running = status.running
  const now = useTicker(running)
  // 空闲时优先用持久化的夜报(带触发方式与「是否手动停止」);服务重启后内存状态是空的,也只能靠它
  const run = !running && lastRun && lastRun.started_at ? { ...lastRun, running: false } : { ...status, trigger: trigger ?? "", stopped: false, running }
  const outcome = outcomeOf(run)
  const seconds = durationSeconds(run.started_at, run.finished_at, now)
  const previous = running && lastRun?.started_at && lastRun.started_at !== status.started_at ? lastRun : null

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle>{running ? "本次运行" : "最近一次运行"}</CardTitle>
          <CardDescription className="truncate text-xs">
            {running ? "园丁正在审查记忆,结果经 CE API 回写" : run.started_at ? `${TRIGGER_LABEL[run.trigger] ?? "运行"}于 ${formatDateTime(run.started_at)}` : "还没有运行过园丁"}
          </CardDescription>
        </div>
        <OutcomeBadge outcome={outcome} />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-col divide-y">
          <Row label="触发方式">{run.trigger ? (TRIGGER_LABEL[run.trigger] ?? run.trigger) : "—"}</Row>
          <Row label="开始于">
            {run.started_at ? (
              <>
                {formatDateTime(run.started_at)}
                <span className="text-muted-foreground">
                  {" "}
                  · <RelativeTime value={run.started_at} />
                </span>
              </>
            ) : (
              "—"
            )}
          </Row>
          <Row label={running ? "已运行" : "耗时"}>{seconds === null ? "—" : formatDuration(seconds)}</Row>
          <Row label="结束于">{running ? <span className="text-muted-foreground">进行中</span> : formatDateTime(run.finished_at)}</Row>
          <Row label="退出码">
            {run.exit_code === null || run.exit_code === undefined ? (
              <span className="text-muted-foreground">—</span>
            ) : (
              <span className={cn("font-mono", run.exit_code === 0 ? "text-emerald-600 dark:text-emerald-400" : "text-destructive")}>{run.exit_code}</span>
            )}
          </Row>
        </div>
        {run.error && outcome !== "stopped" ? (
          <Notice tone="destructive" title={outcome === "timeout" ? "超时终止" : "运行出错"}>
            <span className="font-mono text-xs break-all">{run.error}</span>
          </Notice>
        ) : null}
      </CardContent>
      {previous ? (
        <CardFooter className="text-xs text-muted-foreground">
          上一次:{OUTCOME_LABEL[outcomeOf({ ...previous, running: false })].label} · {formatDateTime(previous.started_at)}
        </CardFooter>
      ) : null}
    </Card>
  )
}

/** 环境自检:把「能不能跑」拆成一项一项,不用靠报错去猜 */
export function ChecksCard({ checks, loading, fetching, onRecheck }: { checks: GardenerCheck[] | undefined; loading: boolean; fetching: boolean; onRecheck: () => void }) {
  const failed = checks?.filter((check) => !check.ok).length ?? 0
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle className="flex items-center gap-2">
            环境自检
            {checks ? failed ? <ToneBadge tone="destructive-light" label={`${failed} 项未通过`} /> : <ToneBadge tone="success-light" label="全部通过" /> : null}
          </CardTitle>
          <CardDescription className="truncate text-xs">运行器、提示词、模型端点、回写令牌与目标仓库</CardDescription>
        </div>
        <Button type="button" size="icon-sm" variant="ghost" aria-label="重新检查" disabled={fetching} onClick={onRecheck}>
          <RefreshCwIcon className={fetching ? "animate-spin" : undefined} />
        </Button>
      </CardHeader>
      <CardContent className="flex flex-col divide-y">
        {loading || !checks
          ? Array.from({ length: 5 }).map((_, index) => (
              <div key={index} className="flex gap-3 py-3">
                <Skeleton className="size-4 rounded-full" />
                <div className="flex flex-1 flex-col gap-1.5">
                  <Skeleton className="h-3.5 w-32" />
                  <Skeleton className="h-3 w-48" />
                </div>
              </div>
            ))
          : checks.map((check) => (
              <div key={check.key} className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
                {check.ok ? (
                  <CircleCheckIcon className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" aria-label="通过" />
                ) : (
                  <CircleXIcon className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="未通过" />
                )}
                <div className="flex min-w-0 flex-col gap-0.5">
                  <span className="text-sm font-medium">{check.label}</span>
                  <span className={cn("text-xs break-all", check.ok ? "text-muted-foreground" : "text-destructive")}>{check.detail}</span>
                </div>
              </div>
            ))}
      </CardContent>
    </Card>
  )
}

/** 外部调度命令:不用内置夜间定时的话,交给宿主机的计划任务跑 */
export function CommandCard({ command }: { command: string }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle className="flex items-center gap-1.5">
            外部调度命令
            <Hint label="外部调度命令">按当前配置生成。两个令牌以占位符出现,填上真实值再放进计划任务;命令本身不会带出任何密钥。</Hint>
          </CardTitle>
          <CardDescription className="truncate text-xs">交给 cron / 任务计划程序定时运行</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        <CodeSnippet title="外部调度命令" code={command} />
      </CardContent>
    </Card>
  )
}
