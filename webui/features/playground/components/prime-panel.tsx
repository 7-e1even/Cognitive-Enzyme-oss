"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation } from "@tanstack/react-query"
import { FlaskConicalIcon, RotateCcwIcon, Trash2Icon, UndoIcon } from "lucide-react"
import { useState, useSyncExternalStore } from "react"
import { useForm } from "react-hook-form"
import { toast } from "sonner"

import { EmptyState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { adminApi } from "@/lib/api/endpoints"
import type { PlaygroundResult } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import { CandidateGrid } from "./candidate-grid"
import { DEFAULT_PARAMS, excerpt, JUDGE_STATUS, loadRuns, MAX_RUNS, saveRuns, type PrimeParams, type PrimeRun } from "./data"
import { PrimeFacts } from "./prime-facts"
import { PrimeForm, primeSchema } from "./prime-form"
import { PrimeTrace } from "./prime-trace"

const timeFormat = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
const noopSubscribe = () => () => {}

/** 服务端与水合那一帧返回 false,之后为 true:本标签页的运行记录只在水合后显示,避免前后两次渲染不一致。 */
function useHydrated() {
  return useSyncExternalStore(noopSubscribe, () => true, () => false)
}

function injectedIds(result: PlaygroundResult) {
  return new Set(result.candidates.filter((c) => c.injected).map((c) => c.id))
}

/** 与上一次「同一情境」的运行比较注入结果:调参数时最想知道的就是推的东西变没变。 */
function RunDiff({ run, previous }: { run: PrimeRun; previous: PrimeRun | undefined }) {
  if (!previous || previous.params.context.trim() !== run.params.context.trim()) return null
  const now = injectedIds(run.result)
  const before = injectedIds(previous.result)
  const added = run.result.candidates.filter((c) => c.injected && !before.has(c.id))
  const removed = previous.result.candidates.filter((c) => c.injected && !now.has(c.id))

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-lg border bg-muted/40 px-3 py-2 text-xs">
      <span className="text-muted-foreground">与运行 #{previous.id}(同一情境)相比:</span>
      {added.length === 0 && removed.length === 0 ? (
        <span className="font-medium">注入结果没有变化</span>
      ) : (
        <>
          {added.map((c) => (
            <ToneBadge key={`+${c.id}`} tone="success-light" label={`+ ${excerpt(c.then, 18)}`} title={c.then} />
          ))}
          {removed.map((c) => (
            <ToneBadge key={`-${c.id}`} tone="destructive-light" label={`− ${excerpt(c.then, 18)}`} title={c.then} />
          ))}
        </>
      )}
    </div>
  )
}

function ResultHeader({ run, onLoad, onRerun, rerunning }: { run: PrimeRun; onLoad: () => void; onRerun: () => void; rerunning: boolean }) {
  const judge = JUDGE_STATUS[run.result.status.judge] ?? JUDGE_STATUS.off
  const p = run.params
  const meta = [
    timeFormat.format(run.at),
    `召回 ${run.result.stages.recalled} · 注入 ${run.result.stages.injected}`,
    `${(run.result.elapsed_ms / 1000).toFixed(2)} 秒`,
    p.domain ? `领域 ${p.domain}` : "",
    p.project ? `项目 ${p.project}` : "",
    `最多 ${p.max_results} 条 · hook ≥ ${p.hook_min_conf.toFixed(2)}`,
  ].filter(Boolean)

  return (
    <header className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-sm text-muted-foreground">运行 #{run.id} /</span>
          <h3 className="max-w-xl truncate text-base font-semibold" title={p.context}>
            {excerpt(p.context, 60)}
          </h3>
          <ToneBadge tone={judge.tone} label={`判官${judge.label}`} />
        </div>
        <p className="text-xs text-muted-foreground tabular-nums">{meta.join(" · ")}</p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button variant="outline" size="sm" onClick={onLoad}>
          <UndoIcon /> 载入参数
        </Button>
        <Button size="sm" onClick={onRerun} disabled={rerunning}>
          <RotateCcwIcon /> 重新运行
        </Button>
      </div>
    </header>
  )
}

export function PrimePanel() {
  const hydrated = useHydrated()
  const form = useForm<PrimeParams>({ resolver: zodResolver(primeSchema), defaultValues: DEFAULT_PARAMS })
  const [runs, setRuns] = useState<PrimeRun[]>(loadRuns)
  const [activeId, setActiveId] = useState<number | null>(null)

  const run = useMutation({
    mutationFn: (params: PrimeParams) =>
      adminApi.playground({
        context: params.context,
        domain: params.domain || undefined,
        project: params.project || undefined,
        max_results: params.max_results,
        hook_min_conf: params.hook_min_conf,
      }),
    onSuccess: (result, params) => {
      setRuns((prev) => {
        const next: PrimeRun = { id: (prev[0]?.id ?? 0) + 1, at: Date.now(), params, result }
        const list = [next, ...prev].slice(0, MAX_RUNS)
        saveRuns(list)
        return list
      })
      setActiveId(null)
    },
  })

  const visibleRuns = hydrated ? runs : []
  const active = visibleRuns.find((r) => r.id === activeId) ?? visibleRuns[0]
  const activeIndex = active ? visibleRuns.indexOf(active) : -1
  const previous = activeIndex >= 0 ? visibleRuns[activeIndex + 1] : undefined

  const loadParams = (target: PrimeRun) => {
    form.reset(target.params)
    toast.success(`已载入运行 #${target.id} 的参数`)
    window.scrollTo({ top: 0, behavior: "smooth" })
  }

  const clearRuns = () => {
    setRuns([])
    saveRuns([])
    setActiveId(null)
  }

  return (
    <div className="@container flex flex-col gap-4">
      <PrimeForm form={form} pending={run.isPending} onRun={(params) => run.mutate(params)} />

      {visibleRuns.length > 1 ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">本页运行记录</span>
          <ToggleGroup
            type="single"
            value={active ? String(active.id) : ""}
            onValueChange={(value) => value && setActiveId(Number(value))}
            variant="outline"
            size="sm"
            className="flex-wrap"
            aria-label="切换查看的运行"
          >
            {visibleRuns.map((r) => (
              <Tooltip key={r.id}>
                <TooltipTrigger asChild>
                  <ToggleGroupItem value={String(r.id)} className="h-7 gap-1.5 px-2.5 text-xs">
                    <span className="font-medium">#{r.id}</span>
                    <span className="text-muted-foreground tabular-nums">
                      注入 {r.result.stages.injected}/{r.result.stages.recalled}
                    </span>
                  </ToggleGroupItem>
                </TooltipTrigger>
                <TooltipContent>
                  {timeFormat.format(r.at)} · {excerpt(r.params.context, 80)}
                </TooltipContent>
              </Tooltip>
            ))}
          </ToggleGroup>
          <Button variant="ghost" size="xs" onClick={clearRuns}>
            <Trash2Icon /> 清空记录
          </Button>
        </div>
      ) : null}

      {run.isPending && !active ? (
        <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-3">
          <Skeleton className="h-96 w-full @4xl:col-span-2" />
          <Skeleton className="h-96 w-full" />
        </div>
      ) : active ? (
        <div className={cn("flex flex-col gap-4 transition-opacity", run.isPending && "opacity-60")}>
          <ResultHeader run={active} onLoad={() => loadParams(active)} onRerun={() => run.mutate(active.params)} rerunning={run.isPending} />
          <RunDiff run={active} previous={previous} />
          <div className="grid grid-cols-1 items-start gap-4 @4xl:grid-cols-3">
            <PrimeTrace result={active.result} className="@4xl:col-span-2" />
            <PrimeFacts result={active.result} />
          </div>
          <CandidateGrid key={active.id} result={active.result} />
        </div>
      ) : (
        <EmptyState
          icon={FlaskConicalIcon}
          title="还没有运行"
          description="输入一段情境,看看线上 prime 会召回哪些记忆、判官怎么打分、最后推送哪几条。"
        />
      )}
    </div>
  )
}
