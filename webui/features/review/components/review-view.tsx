"use client"

import { useQueryClient } from "@tanstack/react-query"
import { format } from "date-fns"
import { DownloadIcon, FunnelXIcon, ListChecksIcon, RefreshCwIcon, ShieldCheckIcon, TrendingDownIcon } from "lucide-react"
import { useCallback, useDeferredValue, useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { DataTable } from "@/components/data-table"
import { OptionSelect } from "@/components/option-select"
import { SearchInput } from "@/components/search-input"
import { EmptyState, ErrorState } from "@/components/states"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { errorMessage } from "@/lib/api/client"
import { enzymeApi } from "@/lib/api/endpoints"
import { qk, useIsAdmin } from "@/lib/api/hooks"
import type { EnzymeRow } from "@/lib/api/types"
import { downloadJson } from "@/lib/download"
import { formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { EnzymeDetailSheet, type ReviewDecision } from "@/features/enzymes/components/enzyme-detail-sheet"
import { ACTIVATE_CONFIDENCE_FLOOR, QUALITY_GATE } from "@/features/enzymes/lib/options"
import { useEnzymeFacets, useInvalidateEnzymes } from "@/features/enzymes/lib/queries"

import { QUEUE_PARAMS, useQuarantineReasons, useReviewQueue, waitingDays, waitingFor } from "../lib/review-data"
import { GatePolicy } from "./gate-policy"
import { RecentDecisions } from "./recent-decisions"
import { createReviewColumns, type ReviewAction } from "./review-queue-columns"
import { ReviewSummary } from "./review-summary"

const EMPTY: EnzymeRow[] = []
const PAGE_SIZE = 10

interface Filters {
  search: string
  origin: string
  domain: string
  lowQuality: boolean
}
const NO_FILTERS: Filters = { search: "", origin: "", domain: "", lowQuality: false }

const DECISION_COPY: Record<ReviewDecision, { single: string; bulk: (count: number) => string; description?: string }> = {
  activate: {
    single: "已通过",
    bulk: (count) => `已通过 ${count} 条记忆`,
    description: `已启用并参与召回,置信度提升到至少 ${ACTIVATE_CONFIDENCE_FLOOR}。`,
  },
  reject: {
    single: "已拒绝",
    bulk: (count) => `已拒绝 ${count} 条记忆`,
    description: "保留在库里,不参与召回;需要时可以在记忆页里重新启用。",
  },
  delete: { single: "已删除", bulk: (count) => `已删除 ${count} 条记忆` },
}

/** 隔离审核:质量门与免疫巡检拦下的可疑记忆,人工确认后才参与召回。 */
export function ReviewView() {
  const queue = useReviewQueue()
  const facets = useEnzymeFacets()
  const { reasons, loading: reasonsLoading } = useQuarantineReasons()
  const invalidate = useInvalidateEnzymes()
  const client = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const isAdmin = useIsAdmin()

  const items = queue.data?.enzymes ?? EMPTY
  const queueTotal = queue.data?.total ?? 0
  const libraryTotal = Object.values(facets.data?.status ?? {}).reduce((total, value) => total + value, 0)
  // 同一次渲染里的时长都按同一个「现在」算;数据刷新时跟着更新
  const now = queue.dataUpdatedAt || 0

  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [page, setPage] = useState(0)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const search = useDeferredValue(filters.search).trim().toLowerCase()
  const patch = (next: Partial<Filters>) => {
    setFilters((current) => ({ ...current, ...next }))
    setPage(0)
  }

  // 队列按「新到旧」(接口已排好),筛选在页面里做,抽屉的上一条 / 下一条和表格走同一个顺序
  const ordered = useMemo(
    () =>
      items.filter((item) => {
        if (filters.origin && item.origin !== filters.origin) return false
        if (filters.domain && item.domain !== filters.domain) return false
        if (filters.lowQuality && !(item.quality_score > 0 && item.quality_score < QUALITY_GATE.refine)) return false
        if (!search) return true
        const reason = reasons.get(item.id)
        return [item.id, item.then, item.because, item.patterns, item.domain, item.tags.join(" "), reason?.full ?? ""].join("\n").toLowerCase().includes(search)
      }),
    [items, filters.origin, filters.domain, filters.lowQuality, search, reasons],
  )
  const orderedIds = useMemo(() => ordered.map((item) => item.id), [ordered])
  const pageCount = Math.max(1, Math.ceil(ordered.length / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const pageRows = ordered.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)

  const originOptions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of items) counts.set(item.origin, (counts.get(item.origin) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, label: `${labelOf.origin(value).label} · ${count}` }))
  }, [items])
  const domainOptions = useMemo(() => {
    const counts = new Map<string, number>()
    for (const item of items) if (item.domain) counts.set(item.domain, (counts.get(item.domain) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([value, count]) => ({ value, label: `${value} · ${count}` }))
  }, [items])

  // ── 详情抽屉:关闭后保留 id,退场动画期间内容还在 ──
  const [activeId, setActiveId] = useState<string | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)
  const openEnzyme = useCallback((id: string) => {
    setActiveId(id)
    setSheetOpen(true)
  }, [])
  const activeIndex = activeId ? orderedIds.indexOf(activeId) : -1

  // ── 决定 ──
  const [pending, setPending] = useState<ReviewDecision | null>(null)
  const decide = useCallback(
    async (decision: ReviewDecision, ids: string[], options: { subject?: string; fromSheet?: boolean } = {}) => {
      if (!ids.length) return
      // 抽屉里的决定已经在抽屉里确认过
      if (!options.fromSheet && decision !== "activate") {
        const what = options.subject ? `「${options.subject}」` : `选中的 ${ids.length} 条记忆`
        const ok = await confirm(
          decision === "delete"
            ? {
                title: options.subject ? "删除这条记忆?" : `删除 ${ids.length} 条记忆?`,
                description: `${what}会从库里移除,且无法恢复。只是不想让它参与召回的话,选「拒绝」即可。`,
                confirmText: "删除",
                destructive: true,
              }
            : {
                title: options.subject ? "拒绝这条记忆?" : `拒绝 ${ids.length} 条记忆?`,
                description: `${what}会保留在库里,但不再参与召回;之后可以在记忆页里重新启用。`,
                confirmText: "拒绝",
                destructive: true,
              },
        )
        if (!ok) return
      }

      // 先算好抽屉接下来看哪一条:同一位置上的下一条,处理的是最后一条就往前退一条
      const remaining = orderedIds.filter((id) => !ids.includes(id))
      const nextId = options.fromSheet && activeIndex >= 0 ? (remaining[Math.min(activeIndex, remaining.length - 1)] ?? null) : null

      setPending(decision)
      try {
        const result = await enzymeApi.bulk(ids, decision)
        // 队列先在本地去掉处理过的记忆,再让服务端数据跟上
        client.setQueryData<{ enzymes: EnzymeRow[]; total: number }>(qk.enzymes(QUEUE_PARAMS), (current) =>
          current
            ? { enzymes: current.enzymes.filter((item) => !result.done.includes(item.id)), total: Math.max(0, current.total - result.done.length) }
            : current,
        )
        if (options.fromSheet) {
          if (nextId) setActiveId(nextId)
          else setSheetOpen(false)
        }
        setSelected((current) => {
          const next = new Set(current)
          for (const id of ids) next.delete(id)
          return next
        })
        const copy = DECISION_COPY[decision]
        if (ids.length === 1 && options.subject) toast.success(`${copy.single}「${options.subject}」`, { description: copy.description })
        else toast.success(copy.bulk(result.done.length), { description: copy.description })
        if (result.missing.length) toast.warning(`${result.missing.length} 条记忆已不存在`, { description: "可能刚被别人处理过,队列已刷新。" })
        if (options.fromSheet && !nextId) toast.info("队列已清空", { description: "质量门没有拦下更多需要审核的记忆。" })
        await invalidate(decision === "delete" ? { removed: ids } : { touched: result.done })
      } catch (error) {
        toast.error("操作失败", { description: errorMessage(error) })
      } finally {
        setPending(null)
      }
    },
    [activeIndex, client, confirm, invalidate, orderedIds],
  )

  const handleRowAction = useCallback(
    (action: ReviewAction, enzyme: EnzymeRow) => {
      void decide(action === "approve" ? "activate" : "reject", [enzyme.id], { subject: enzyme.then })
    },
    [decide],
  )

  const toggle = useCallback((id: string) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])
  const toggleAll = useCallback((ids: string[], checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current)
      for (const id of ids) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }, [])

  const columns = useMemo(
    () => createReviewColumns({ selected, onToggle: toggle, onToggleAll: toggleAll, onAction: handleRowAction, reasons, reasonsLoading, busy: pending !== null, now }),
    [selected, toggle, toggleAll, handleRowAction, reasons, reasonsLoading, pending, now],
  )

  // 被筛掉的行不算在选择里:看不见的东西不该被一起处理
  const visibleIds = useMemo(() => new Set(orderedIds), [orderedIds])
  const selectedIds = [...selected].filter((id) => visibleIds.has(id))
  const hasFilters = Boolean(filters.search.trim() || filters.origin || filters.domain || filters.lowQuality)

  const handleExport = () =>
    downloadJson(
      `review-queue-${format(new Date(), "yyyyMMdd-HHmm")}.json`,
      {
        exported_at: new Date().toISOString(),
        total: ordered.length,
        items: ordered.map((item) => ({ ...item, quarantine_reason: reasons.get(item.id)?.full ?? null })),
      },
      "审核队列",
    )

  const oldest = items.at(-1)
  const figures = queue.data
    ? [`${formatNumber(queueTotal)} 条待审核`, ...(oldest ? [`最久已等 ${waitingFor(oldest.created_at, now)}`] : [])].join(" · ")
    : ""
  const fresh = items.filter((item) => waitingDays(item.created_at, now) < 1).length

  if (queue.isError && !queue.data) return <ErrorState error={queue.error} onRetry={() => void queue.refetch()} title="审核队列加载失败" />

  return (
    <div className="@container flex flex-col gap-6">
      <ReviewSummary items={items} queueTotal={queueTotal} libraryTotal={libraryTotal} loading={queue.isPending} now={now} />

      {!queue.isPending && !items.length ? (
        <EmptyState
          icon={ShieldCheckIcon}
          title="质量门没有拦下需要审核的记忆"
          description="新沉淀的经验如果质量分不到自动通过线,或被免疫巡检判为可疑,会先停在这里等人确认。"
        />
      ) : (
        <section className="flex flex-col gap-3" aria-label="审核队列">
          <div className="flex flex-wrap items-center gap-2">
            <SearchInput value={filters.search} onChange={(search) => patch({ search })} placeholder="搜索内容、触发词或原因" label="搜索待审记忆" />
            <OptionSelect label="按来源筛选" value={filters.origin} allLabel="全部来源" options={originOptions} onChange={(origin) => patch({ origin })} className="w-36" />
            <OptionSelect label="按领域筛选" value={filters.domain} allLabel="全部领域" options={domainOptions} onChange={(domain) => patch({ domain })} className="w-36" />
            <Button
              size="sm"
              variant="outline"
              aria-pressed={filters.lowQuality}
              className="aria-pressed:bg-muted"
              onClick={() => patch({ lowQuality: !filters.lowQuality })}
            >
              <TrendingDownIcon /> 低质量
            </Button>
            {hasFilters ? (
              <Button size="sm" variant="ghost" onClick={() => patch(NO_FILTERS)}>
                <FunnelXIcon /> 清除筛选
              </Button>
            ) : null}
            <div className="ml-auto flex items-center gap-2">
              <span className="hidden text-sm text-muted-foreground tabular-nums sm:inline">
                {hasFilters ? `显示 ${formatNumber(ordered.length)} / ${formatNumber(queueTotal)} 条` : fresh ? `${figures} · 24 小时内新增 ${formatNumber(fresh)}` : figures}
              </span>
              <Button size="icon-sm" variant="ghost" aria-label="刷新队列" disabled={queue.isFetching} onClick={() => { void queue.refetch(); void facets.refetch() }}>
                <RefreshCwIcon className={queue.isFetching ? "animate-spin" : undefined} />
              </Button>
              <Button size="sm" variant="outline" disabled={!ordered.length} onClick={handleExport}>
                <DownloadIcon /> 导出
              </Button>
              <Button size="sm" disabled={!ordered.length} onClick={() => ordered[0] && openEnzyme(ordered[0].id)}>
                <ListChecksIcon /> 逐条审核
              </Button>
            </div>
          </div>

          {selectedIds.length ? (
            <div role="region" aria-label="批量审核" className="flex flex-col gap-3 rounded-lg border bg-muted/40 px-3 py-2 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm font-medium">已选 {formatNumber(selectedIds.length)} 条待审记忆</span>
                <span className="text-xs text-muted-foreground">通过会启用并把置信度提升到至少 {ACTIVATE_CONFIDENCE_FLOOR};拒绝的保留在库里但不参与召回。</span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button size="sm" variant="ghost" className="text-destructive hover:text-destructive" disabled={pending !== null} onClick={() => void decide("delete", selectedIds)}>
                  {pending === "delete" ? <Spinner /> : null}删除 {formatNumber(selectedIds.length)}
                </Button>
                <Button size="sm" variant="outline" disabled={pending !== null} onClick={() => void decide("reject", selectedIds)}>
                  {pending === "reject" ? <Spinner /> : null}拒绝 {formatNumber(selectedIds.length)}
                </Button>
                <Button size="sm" disabled={pending !== null} onClick={() => void decide("activate", selectedIds)}>
                  {pending === "activate" ? <Spinner /> : null}通过 {formatNumber(selectedIds.length)}
                </Button>
                <Button size="sm" variant="ghost" disabled={pending !== null} onClick={() => setSelected(new Set())}>
                  取消选择
                </Button>
              </div>
            </div>
          ) : null}

          <DataTable
            columns={columns}
            data={pageRows}
            loading={queue.isPending}
            empty="没有符合条件的待审记忆。"
            onRowClick={(row) => openEnzyme(row.id)}
            page={safePage}
            pageSize={PAGE_SIZE}
            total={ordered.length}
            onPageChange={setPage}
          />
          {queueTotal > items.length ? (
            <p className="text-xs text-muted-foreground">队列里共有 {formatNumber(queueTotal)} 条,这里只显示最新的 {formatNumber(items.length)} 条。</p>
          ) : null}
        </section>
      )}

      <div className="grid grid-cols-1 gap-4 @4xl:grid-cols-2">
        {isAdmin ? <RecentDecisions onOpenEnzyme={openEnzyme} /> : null}
        <GatePolicy className={isAdmin ? undefined : "@4xl:col-span-2"} />
      </div>

      <EnzymeDetailSheet
        enzymeId={activeId}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
        onOpenEnzyme={openEnzyme}
        review={{
          position: activeIndex >= 0 ? { index: activeIndex, total: orderedIds.length } : null,
          onPrev: activeIndex > 0 ? () => setActiveId(orderedIds[activeIndex - 1]) : null,
          onNext: activeIndex >= 0 && activeIndex < orderedIds.length - 1 ? () => setActiveId(orderedIds[activeIndex + 1]) : null,
          onDecide: (decision, enzyme) => void decide(decision, [enzyme.id], { subject: enzyme.suggestion, fromSheet: true }),
          pending,
        }}
      />
      {confirmDialog}
    </div>
  )
}
