"use client"

import { format } from "date-fns"
import { ArrowDownUpIcon, DownloadIcon, FunnelXIcon, PlusIcon } from "lucide-react"
import { useCallback, useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { DataTable } from "@/components/data-table"
import { OptionSelect } from "@/components/option-select"
import { SearchInput } from "@/components/search-input"
import { ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { errorMessage } from "@/lib/api/client"
import { enzymeApi } from "@/lib/api/endpoints"
import type { BulkAction, EnzymeRow } from "@/lib/api/types"
import { downloadJson } from "@/lib/download"
import { formatNumber } from "@/lib/format"

import {
  MIN_CONFIDENCE_STEPS,
  ORIGIN_OPTIONS,
  SEVERITY_OPTIONS,
  SORT_KEYS,
  SORT_LABEL,
  STATUS_OPTIONS,
  type SortKey,
} from "../lib/options"
import { useBulkEnzymes, useEnzymeFacets, useEnzymeList } from "../lib/queries"
import { CLEARED_FILTERS, DEFAULT_ORDER, DEFAULT_SORT, hasActiveFilters, toListParams, useEnzymeViewState } from "../lib/view-state"
import { createEnzymeColumns } from "./enzyme-columns"
import { EnzymeDetailSheet } from "./enzyme-detail-sheet"
import { EnzymeFormSheet } from "./enzyme-form-sheet"

const ACTION_COPY: Record<BulkAction, { title: string; description?: string }> = {
  activate: { title: "已启用", description: "置信度低于 0.4 的已提升到 0.4,开始参与召回。" },
  quarantine: { title: "已送回隔离", description: "在「隔离审核」里等待人工确认,期间不参与召回。" },
  reject: { title: "已拒绝", description: "保留在库里,不再参与召回。" },
  delete: { title: "已删除" },
}

const EMPTY: EnzymeRow[] = []

/** 记忆:检索、筛选、批量处理与逐条编辑。筛选、排序、页码和打开的记忆都在 URL 里。 */
export function EnzymeManager() {
  const [view, setView] = useEnzymeViewState()
  const params = useMemo(() => toListParams(view), [view])
  const list = useEnzymeList(params)
  const facets = useEnzymeFacets()
  const bulk = useBulkEnzymes()
  const { confirm, confirmDialog } = useConfirm()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [exporting, setExporting] = useState(false)
  // 关闭后保留最后打开的一条,退场动画期间内容不消失
  const [shownId, setShownId] = useState(view.id)
  if (view.id && view.id !== shownId) setShownId(view.id)

  const rows = list.data?.enzymes ?? EMPTY
  const total = list.data?.total ?? 0
  const filtered = hasActiveFilters(view)

  const domainOptions = useMemo(
    () =>
      Object.entries(facets.data?.domain ?? {})
        .filter(([name]) => name)
        .sort((a, b) => b[1] - a[1])
        .map(([name, count]) => ({ value: name, label: `${name} · ${formatNumber(count)}` })),
    [facets.data],
  )
  const statusOptions = STATUS_OPTIONS.map((option) => {
    const count = facets.data?.status[option.value]
    return { ...option, label: count === undefined ? option.label : `${option.label} · ${formatNumber(count)}` }
  })

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
  const columns = useMemo(() => createEnzymeColumns({ selected, onToggle: toggle, onToggleAll: toggleAll }), [selected, toggle, toggleAll])

  /** 改筛选条件时回到第一页并清空选择。 */
  const change = (patch: Parameters<typeof setView>[0]) => {
    setSelected(new Set())
    setView({ page: 1, ...patch })
  }

  async function runBulk(action: BulkAction) {
    const ids = [...selected]
    if (!ids.length) return
    if (action === "delete" || action === "reject") {
      const ok = await confirm(
        action === "delete"
          ? {
              title: `删除 ${ids.length} 条记忆?`,
              description: "选中的记忆会从库里移除,之后不再参与召回,且无法恢复。只是不想让它们被召回的话,改为「拒绝」即可。",
              confirmText: "删除",
              destructive: true,
            }
          : {
              title: `拒绝 ${ids.length} 条记忆?`,
              description: "会保留在库里,但不再参与召回;之后可以随时重新启用。",
              confirmText: "拒绝",
              destructive: true,
            },
      )
      if (!ok) return
    }
    try {
      const result = await bulk.mutateAsync({ ids, action })
      const copy = ACTION_COPY[action]
      toast.success(`${copy.title} ${result.done.length} 条`, { description: copy.description })
      if (result.missing.length) toast.warning(`${result.missing.length} 条记忆已不存在`, { description: "可能刚被删除或合并,列表已刷新。" })
      setSelected(new Set())
    } catch (error) {
      toast.error("操作失败", { description: errorMessage(error) })
    }
  }

  async function handleExport() {
    setExporting(true)
    try {
      const base = { ...toListParams(view), limit: 1000 }
      const enzymes: EnzymeRow[] = []
      let count = 0
      do {
        const page = await enzymeApi.list({ ...base, offset: enzymes.length })
        count = page.total
        enzymes.push(...page.enzymes)
        if (!page.enzymes.length) break
      } while (enzymes.length < count)
      downloadJson(`enzymes-${format(new Date(), "yyyyMMdd-HHmm")}.json`, { exported_at: new Date().toISOString(), total: count, enzymes }, "记忆")
    } catch (error) {
      toast.error("导出失败", { description: errorMessage(error) })
    } finally {
      setExporting(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput value={view.q} onChange={(q) => change({ q })} placeholder="搜索建议、触发词或原因" label="搜索记忆" />
        <OptionSelect label="按状态筛选" value={view.status} allLabel="全部状态" options={statusOptions} onChange={(status) => change({ status })} className="w-32" />
        <OptionSelect label="按领域筛选" value={view.domain} allLabel="全部领域" options={domainOptions} onChange={(domain) => change({ domain })} className="w-36" />
        <OptionSelect label="按严重度筛选" value={view.severity} allLabel="全部严重度" options={SEVERITY_OPTIONS} onChange={(severity) => change({ severity })} className="w-32" />
        <OptionSelect label="按来源筛选" value={view.origin} allLabel="全部来源" options={ORIGIN_OPTIONS} onChange={(origin) => change({ origin })} className="w-32" />
        <OptionSelect
          label="置信度下限"
          value={view.minConfidence === null ? "" : String(view.minConfidence)}
          allLabel="置信度不限"
          options={MIN_CONFIDENCE_STEPS.map((step) => ({ value: String(step), label: `≥ ${step}` }))}
          onChange={(value) => change({ minConfidence: value ? Number(value) : null })}
          className="w-32"
        />
        <OptionSelect
          label="排序"
          value={view.sort}
          options={SORT_KEYS.map((key) => ({ value: key, label: SORT_LABEL[key] }))}
          onChange={(sort) => change({ sort: sort as SortKey, order: "desc" })}
          className="w-32"
        />
        <Button
          variant="outline"
          size="icon-sm"
          aria-label={view.order === "desc" ? "当前从高到低,点击改为从低到高" : "当前从低到高,点击改为从高到低"}
          title={view.order === "desc" ? "从高到低" : "从低到高"}
          onClick={() => change({ order: view.order === "desc" ? "asc" : "desc" })}
        >
          <ArrowDownUpIcon />
        </Button>
        {filtered ? (
          <Button variant="ghost" size="sm" onClick={() => change({ ...CLEARED_FILTERS, sort: DEFAULT_SORT, order: DEFAULT_ORDER })}>
            <FunnelXIcon /> 清除筛选
          </Button>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" disabled={exporting || !total} onClick={() => void handleExport()}>
            <DownloadIcon /> 导出
          </Button>
          <Button size="sm" onClick={() => setView({ create: true })}>
            <PlusIcon /> 新建记忆
          </Button>
        </div>
      </div>

      {selected.size ? (
        <div role="region" aria-label="批量操作" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/40 px-3 py-2">
          <span className="text-sm font-medium">已选 {formatNumber(selected.size)} 条</span>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={bulk.isPending} onClick={() => void runBulk("activate")}>
              启用
            </Button>
            <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => void runBulk("quarantine")}>
              送回隔离
            </Button>
            <Button size="sm" variant="outline" disabled={bulk.isPending} onClick={() => void runBulk("reject")}>
              拒绝
            </Button>
            <Button size="sm" variant="destructive" disabled={bulk.isPending} onClick={() => void runBulk("delete")}>
              删除
            </Button>
            <Button size="sm" variant="ghost" disabled={bulk.isPending} onClick={() => setSelected(new Set())}>
              取消选择
            </Button>
          </div>
        </div>
      ) : null}

      {list.isError && !list.data ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} title="记忆加载失败" />
      ) : (
        <DataTable
          columns={columns}
          data={rows}
          loading={list.isPending}
          empty={filtered ? "没有符合条件的记忆。" : "还没有记忆。"}
          onRowClick={(row) => setView({ id: row.id })}
          page={view.page - 1}
          pageSize={view.size}
          total={total}
          onPageChange={(page) => {
            setSelected(new Set())
            setView({ page: page + 1 })
          }}
        />
      )}

      <EnzymeDetailSheet
        enzymeId={view.id ?? shownId}
        open={Boolean(view.id)}
        onOpenChange={(open) => {
          if (!open) setView({ id: null })
        }}
        onOpenEnzyme={(id) => setView({ id })}
        onDeleted={() => setSelected(new Set())}
      />
      <EnzymeFormSheet
        open={view.create}
        onOpenChange={(open) => setView({ create: open })}
        domains={facets.data?.domain ?? {}}
        onCreated={(id) => setView({ ...CLEARED_FILTERS, sort: DEFAULT_SORT, order: DEFAULT_ORDER, page: 1, create: false, id })}
      />
      {confirmDialog}
    </div>
  )
}
