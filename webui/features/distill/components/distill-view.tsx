"use client"

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArchiveIcon, DatabaseIcon, FunnelXIcon, RefreshCwIcon } from "lucide-react"
import { useCallback, useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { DataTable } from "@/components/data-table"
import { OptionSelect } from "@/components/option-select"
import { EmptyState, ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { DecisionRow } from "@/lib/api/types"
import { formatBytes, formatNumber } from "@/lib/format"
import { DECISION_SOURCE, labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { createDecisionColumns } from "./decision-columns"
import { DecisionSheet } from "./decision-sheet"
import { DecisionLogBadge, DistillStatsCards } from "./distill-stats"
import { useDistillParams } from "./use-distill-params"

const JUDGE_OPTIONS = [
  { value: "jev", label: "Jev 判官" },
  { value: "dosage", label: "固定剂量" },
]

/**
 * 判定记录:每次 prime 的判定、事后真实结果与老师标注,是训练团队自己的 System 1 判官所用的数据。
 * 列表是服务端分页的;筛选、页码与打开的记录都在 URL 里,可以直接分享到某一条。
 */
export function DistillView() {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const { params, setParams } = useDistillParams()
  const { source, judge, page, size, decision } = params

  const stats = useQuery({ queryKey: qk.distill, queryFn: adminApi.distillStats, refetchInterval: 60_000 })
  const list = useQuery({
    queryKey: qk.decisions({ source, judge, limit: size, offset: (page - 1) * size }),
    queryFn: () => adminApi.decisions({ source: source || undefined, judge: judge || undefined, limit: size, offset: (page - 1) * size }),
    placeholderData: keepPreviousData,
  })

  const archive = useMutation({
    mutationFn: adminApi.archiveNow,
    onSuccess: (result) => {
      const n = (key: string) => (typeof result[key] === "number" ? (result[key] as number) : 0)
      toast.success("归档完成", {
        description: `判定 ${formatNumber(n("decision_log"))} 条 · 结果 ${formatNumber(n("decision_outcomes"))} 条 · 标注 ${formatNumber(n("teacher_labels"))} 条`,
      })
      void queryClient.invalidateQueries({ queryKey: qk.distill })
    },
    onError: (error) => toast.error("归档失败", { description: errorMessage(error) }),
  })

  const s = stats.data
  const runArchive = async () => {
    const ok = await confirm({
      title: "立即归档判定数据?",
      description: `把上次归档之后新增的判定记录、事后结果与老师标注追加写入 ${s?.archive.dir || "归档目录"};同时清理库里已归档且超过保留期(${s?.archive.keep_days ?? 90} 天)的记录。归档本身不删除任何未归档的数据。`,
      confirmText: "立即归档",
    })
    if (ok) archive.mutate()
  }

  const items = useMemo(() => list.data?.items ?? [], [list.data])
  const total = list.data?.total ?? 0
  const columns = useMemo(() => createDecisionColumns(), [])

  // 关掉面板时 URL 里的 id 立刻清空,但关闭动画还要画原来那条:记住最后打开的一条(渲染期对齐,不用 effect)
  const [shownDecision, setShownDecision] = useState(decision)
  if (decision && decision !== shownDecision) setShownDecision(decision)

  // 详情面板里的上一条 / 下一条:只在当前页里走,到边界就停
  const index = items.findIndex((row) => row.id === decision)
  const prev = index > 0 ? items[index - 1] : undefined
  const next = index >= 0 && index < items.length - 1 ? items[index + 1] : undefined

  const openDecision = useCallback((row: DecisionRow) => setParams({ decision: row.id }), [setParams])

  const sourceOptions = Array.from(new Set([...Object.keys(DECISION_SOURCE), ...Object.keys(s?.by_source ?? {})])).map((key) => ({
    value: key,
    label: labelOf.decisionSource(key).label,
  }))
  const filtered = Boolean(source || judge)
  const refreshing = stats.isFetching || list.isFetching

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground tabular-nums">
          {s ? (
            <>
              <DecisionLogBadge on={s.decision_log} />
              <span>判定 {formatNumber(s.decisions)} 条</span>
              {s.decisions_with_direct_outcomes ? <span>带真实结果 {formatNumber(s.decisions_with_direct_outcomes)} 条</span> : null}
              <span>库内占用 {s.storage_mb.toFixed(1)} MB</span>
              <span>{s.archive.enabled ? `已归档 ${formatBytes(s.archive.bytes ?? 0)}` : "归档已关闭"}</span>
            </>
          ) : null}
        </p>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              void stats.refetch()
              void list.refetch()
            }}
            disabled={refreshing}
          >
            <RefreshCwIcon className={cn(refreshing && "animate-spin")} aria-hidden="true" />
            刷新
          </Button>
          {s && !s.archive.enabled ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span tabIndex={0} className="inline-flex">
                  <Button size="sm" disabled>
                    <ArchiveIcon aria-hidden="true" />
                    立即归档
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent>归档已关闭:在运行参数里设置 distill.archive_dir 后才能归档。</TooltipContent>
            </Tooltip>
          ) : (
            <Button size="sm" onClick={() => void runArchive()} disabled={!s || archive.isPending}>
              <ArchiveIcon aria-hidden="true" />
              {archive.isPending ? "归档中…" : "立即归档"}
            </Button>
          )}
        </div>
      </div>

      {stats.isError && !s ? <ErrorState error={stats.error} onRetry={() => void stats.refetch()} title="数据概况加载失败" /> : <DistillStatsCards stats={s} />}

      {list.isError && !list.data ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} title="判定记录加载失败" />
      ) : !list.isPending && total === 0 && !filtered ? (
        <EmptyState
          icon={DatabaseIcon}
          title="还没有判定记录"
          description={
            s && !s.decision_log
              ? "判定记录已关闭(运行参数 distill.decision_log)。开启后,每次 prime 都会记一条。"
              : "智能体每次提问前调用 prime,这里就会多一条记录。可以先在「召回调试」里试跑,但调试台不写判定记录。"
          }
        />
      ) : (
        <section className="flex flex-col gap-3" aria-label="判定记录">
          <div className="flex flex-wrap items-center gap-2">
            <OptionSelect label="按来源筛选" value={source} allLabel="全部来源" options={sourceOptions} onChange={(value) => setParams({ source: value })} className="w-36" />
            <OptionSelect label="按判官筛选" value={judge} allLabel="全部判官" options={JUDGE_OPTIONS} onChange={(value) => setParams({ judge: value })} className="w-32" />
            {filtered ? (
              <Button variant="ghost" size="sm" onClick={() => setParams({ source: "", judge: "" })}>
                <FunnelXIcon /> 清除筛选
              </Button>
            ) : null}
          </div>
          <div className={cn("transition-opacity", list.isPlaceholderData && "opacity-60")}>
            <DataTable
              columns={columns}
              data={items}
              loading={list.isPending}
              empty="没有符合条件的判定记录。换一个来源或判官试试。"
              onRowClick={openDecision}
              page={page - 1}
              pageSize={size}
              total={total}
              onPageChange={(next) => setParams({ page: next + 1 })}
            />
          </div>
        </section>
      )}

      <DecisionSheet
        decisionId={decision || shownDecision}
        open={Boolean(decision)}
        onOpenChange={(open) => {
          if (!open) setParams({ decision: "" })
        }}
        onPrev={prev ? () => setParams({ decision: prev.id }) : undefined}
        onNext={next ? () => setParams({ decision: next.id }) : undefined}
      />
      {confirmDialog}
    </div>
  )
}
