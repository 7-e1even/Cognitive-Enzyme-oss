"use client"

import { keepPreviousData, useMutation, useQueries, useQuery } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { ArrowRightIcon, DownloadIcon, LayersIcon, RefreshCwIcon, ScrollTextIcon, SearchIcon, UserIcon, XIcon } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import { DataTable } from "@/components/data-table"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription } from "@/components/ui/card"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from "@/components/ui/select"
import { avatarSrc } from "@/features/users/lib/users"
import { intParam, useUrlState } from "@/hooks/use-url-state"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { AuditEntry } from "@/lib/api/types"
import { formatDateTime, formatNumber, formatRelative, initials } from "@/lib/format"
import { cn } from "@/lib/utils"

import { AuditSheet } from "./audit-sheet"
import {
  actionLabel,
  actionTone,
  AUDIT_CATEGORIES,
  categoryMeta,
  categoryOf,
  CONFIG_CATEGORIES,
  summarize,
  TONE_DOT,
} from "./lib/audit"
import { fileStamp, saveJson } from "./lib/save-json"

const PAGE_SIZES = [20, 50, 100] as const
/** 统计的取数窗口:后端单页上限就是 500 */
const STATS_WINDOW = 500
const EXPORT_LIMIT = 500
const DAY = 86_400_000

/**
 * 近期概况:从最近一批审计记录里数出来(后端没有按时间聚合的接口)。只显示有数的项。
 * 窗口装满且最老一条仍在统计区间内时,真实数字只会更大,这时标成 "500+" 而不是把截断后的数当全部。
 */
function recentFacts(items: AuditEntry[], total: number) {
  const now = Date.now()
  const startOfToday = new Date()
  startOfToday.setHours(0, 0, 0, 0)
  const todayStart = startOfToday.getTime()
  const weekStart = now - 7 * DAY
  const time = (entry: AuditEntry) => new Date(entry.created_at).getTime()
  const week = items.filter((entry) => time(entry) >= weekStart)
  const today = items.filter((entry) => time(entry) >= todayStart)
  const config = week.filter((entry) => CONFIG_CATEGORIES.has(categoryOf(entry.action)))
  const actors = new Set(week.map((entry) => entry.actor || "系统"))
  const windowFull = items.length >= STATS_WINDOW && total > items.length
  const oldest = items.length ? time(items[items.length - 1]) : now
  const plus = (count: number, cut: boolean) => `${formatNumber(count)}${cut ? "+" : ""}`
  return [
    { label: "今日", value: plus(today.length, windowFull && oldest >= todayStart), count: today.length },
    { label: "近 7 天", value: plus(week.length, windowFull && oldest >= weekStart), count: week.length },
    { label: "近 7 天的配置变更", value: plus(config.length, windowFull && oldest >= weekStart), count: config.length },
    { label: "近 7 天的操作人", value: formatNumber(actors.size), count: week.length ? actors.size : 0 },
  ].filter((fact) => fact.count > 0)
}

/**
 * 审计日志:谁在什么时候改了什么。筛选、页码与打开的记录都在 URL 里;记录每 30 秒自动刷新——
 * 别的管理员此刻也可能在改东西。
 */
export function AuditView() {
  const [params, setParams] = useUrlState()
  const categoryParam = params.get("category") ?? ""
  const category = AUDIT_CATEGORIES.some((item) => item.value === categoryParam) ? categoryParam : ""
  const actor = params.get("actor") ?? ""
  const query = params.get("q") ?? ""
  const page = Math.max(1, intParam(params.get("page"), 1))
  const size = intParam(params.get("size"), PAGE_SIZES[0], PAGE_SIZES)
  const eventParam = params.get("event")

  const usersQuery = useQuery({ queryKey: qk.users, queryFn: adminApi.users, select: (data) => data.users })
  const users = useMemo(() => usersQuery.data ?? [], [usersQuery.data])
  const usersByName = useMemo(() => new Map(users.map((user) => [user.username, user])), [users])

  const listParams = {
    limit: size,
    offset: (page - 1) * size,
    action: category || undefined,
    actor: actor || undefined,
    q: query || undefined,
  }
  const list = useQuery({
    queryKey: qk.audit(listParams),
    queryFn: () => adminApi.audit(listParams),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  // 概况看的是全局近况,不受下面表格筛选的影响
  const stats = useQuery({
    queryKey: qk.audit({ window: STATS_WINDOW }),
    queryFn: () => adminApi.audit({ limit: STATS_WINDOW }),
    refetchInterval: 60_000,
  })
  // 每个分类的累计条数:后端的 total 就是精确值,只要 limit=1 取一次
  const categoryCounts = useQueries({
    queries: AUDIT_CATEGORIES.map((item) => ({
      queryKey: qk.audit({ count: item.value }),
      queryFn: () => adminApi.audit({ action: item.value, limit: 1 }),
      staleTime: 30_000,
      refetchInterval: 60_000,
    })),
  })
  const countOf = (value: string) => categoryCounts[AUDIT_CATEGORIES.findIndex((item) => item.value === value)]?.data?.total

  // 抽屉:记下打开的那条本身(翻页、自动刷新后它可能不在当前页了),URL 只存编号
  const [openEntry, setOpenEntry] = useState<AuditEntry | null>(null)
  const pageItems = list.data?.items
  const linked =
    eventParam && openEntry?.id !== Number(eventParam)
      ? (pageItems?.find((item) => String(item.id) === eventParam) ?? stats.data?.items.find((item) => String(item.id) === eventParam) ?? null)
      : null
  if (linked && linked !== openEntry) setOpenEntry(linked)
  const sheetOpen = Boolean(eventParam) && Boolean(openEntry)
  const hasFilters = Boolean(category || actor || query)

  const handleOpen = useCallback(
    (entry: AuditEntry) => {
      setOpenEntry(entry)
      setParams({ event: entry.id })
    },
    [setParams],
  )
  const clearFilters = useCallback(() => setParams({ category: null, actor: null, q: null, page: null }), [setParams])

  // 搜索框:输入先留在本地,停顿后再写进 URL;外面清除关键词时输入框跟着对齐
  const [search, setSearch] = useState(query)
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [seenQuery, setSeenQuery] = useState(query)
  if (seenQuery !== query) {
    setSeenQuery(query)
    if (query !== search.trim()) setSearch(query)
  }
  useEffect(() => () => {
    if (searchTimer.current) clearTimeout(searchTimer.current)
  }, [])

  const exportLog = useMutation({
    mutationFn: () => adminApi.audit({ limit: EXPORT_LIMIT, action: category || undefined, actor: actor || undefined, q: query || undefined }),
    onSuccess: (data) =>
      saveJson(
        `audit-log-${fileStamp()}.json`,
        {
          exported_at: new Date().toISOString(),
          filters: { category: category || null, actor: actor || null, q: query || null },
          total: data.total,
          included: data.items.length,
          items: data.items,
        },
        data.total > data.items.length ? `最近 ${data.items.length} 条审计记录` : `${data.items.length} 条审计记录`,
      ),
  })

  const total = list.data?.total ?? 0
  const overall = stats.data?.total
  const facts = recentFacts(stats.data?.items ?? [], stats.data?.total ?? 0)

  const actorOptions = useMemo(() => {
    const options = users.map((user) => ({ value: user.username, label: `${user.display_name}(@${user.username})` }))
    if (actor && !users.some((user) => user.username === actor)) options.push({ value: actor, label: `@${actor}(已删除)` })
    return options
  }, [actor, users])

  const columns = useMemo<ColumnDef<AuditEntry, unknown>[]>(
    () => [
      {
        id: "event",
        header: "操作",
        cell: ({ row }) => {
          const entry = row.original
          return (
            <div className="flex min-w-0 flex-col gap-0.5">
              <span className="truncate text-sm font-medium">{actionLabel(entry.action)}</span>
              <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                <span className={cn("size-1.5 shrink-0 rounded-full", TONE_DOT[actionTone(entry.action)])} aria-hidden="true" />
                <span className="tabular-nums">#{entry.id}</span>
                <span className="truncate font-mono">{entry.action}</span>
              </span>
            </div>
          )
        },
      },
      {
        id: "category",
        header: "分类",
        cell: ({ row }) => {
          const meta = categoryMeta(categoryOf(row.original.action))
          const Icon = meta.icon
          return (
            <Badge variant="outline" className="gap-1">
              <Icon aria-hidden="true" />
              <span className="truncate">{meta.label}</span>
            </Badge>
          )
        },
      },
      {
        id: "actor",
        header: "操作人",
        cell: ({ row }) => {
          const entry = row.original
          const user = usersByName.get(entry.actor)
          const name = user?.display_name || entry.actor || "系统"
          return (
            <div className="flex min-w-0 items-center gap-2">
              <Avatar className="size-7">
                {user?.avatar_url ? <AvatarImage src={avatarSrc(user.avatar_url)} alt={name} /> : null}
                <AvatarFallback className="text-[11px]">{initials(name)}</AvatarFallback>
              </Avatar>
              <div className="min-w-0">
                <div className="truncate text-sm">{name}</div>
                {entry.actor && entry.actor !== name ? <div className="truncate font-mono text-xs text-muted-foreground">@{entry.actor}</div> : null}
              </div>
            </div>
          )
        },
      },
      {
        id: "target",
        header: "对象与明细",
        cell: ({ row }) => {
          const entry = row.original
          const summary = summarize(entry)
          return (
            <div className="flex min-w-0 flex-col gap-0.5">
              {entry.target ? <span className="truncate font-mono text-xs">{entry.target}</span> : null}
              {summary ? <span className="line-clamp-2 text-xs text-muted-foreground">{summary}</span> : null}
              {!entry.target && !summary ? <span className="text-muted-foreground">—</span> : null}
            </div>
          )
        },
      },
      {
        id: "time",
        header: "时间",
        cell: ({ row }) => (
          <div className="flex flex-col text-xs">
            <span className="tabular-nums">{formatDateTime(row.original.created_at)}</span>
            <span className="text-muted-foreground">{formatRelative(row.original.created_at)}</span>
          </div>
        ),
      },
      {
        id: "open",
        header: () => <span className="sr-only">详情</span>,
        cell: () => <ArrowRightIcon className="size-4 text-muted-foreground" aria-hidden="true" />,
      },
    ],
    [usersByName],
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {overall === undefined ? "正在统计…" : `共 ${formatNumber(overall)} 条记录`}
          {hasFilters && list.data ? ` · 筛选后 ${formatNumber(total)} 条` : ""}
        </p>
        <div className="flex items-center gap-2">
          <Button size="sm" type="button" variant="outline" disabled={exportLog.isPending} onClick={() => exportLog.mutate()}>
            {exportLog.isPending ? <Spinner /> : <DownloadIcon />} 导出
          </Button>
          <Button
            size="icon-sm"
            type="button"
            variant="outline"
            aria-label="刷新"
            disabled={list.isFetching}
            onClick={() => {
              void list.refetch()
              void stats.refetch()
            }}
          >
            <RefreshCwIcon className={list.isFetching ? "animate-spin" : undefined} aria-hidden="true" />
          </Button>
        </div>
      </div>

      {facts.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {facts.map((fact) => (
            <Card key={fact.label} className="gap-1 py-3">
              <CardContent className="flex flex-col gap-1 px-4">
                <CardDescription>{fact.label}</CardDescription>
                <span className="text-xl font-semibold tabular-nums">{fact.value}</span>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Select value={category || "all"} onValueChange={(value) => setParams({ category: !value || value === "all" ? null : value, page: null })}>
          <SelectTrigger size="sm" className="w-[168px]" aria-label="按分类筛选">
            <LayersIcon className="text-muted-foreground" aria-hidden="true" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">全部分类</SelectItem>
            </SelectGroup>
            <SelectSeparator />
            <SelectGroup>
              {AUDIT_CATEGORIES.map((item) => {
                const Icon = item.icon
                const count = countOf(item.value)
                return (
                  <SelectItem key={item.value} value={item.value}>
                    <span className="flex w-full items-center justify-between gap-4">
                      <span className="flex items-center gap-2">
                        <Icon className="text-muted-foreground" aria-hidden="true" />
                        {item.label}
                      </span>
                      <span className="text-xs tabular-nums text-muted-foreground">{count === undefined ? "" : formatNumber(count)}</span>
                    </span>
                  </SelectItem>
                )
              })}
            </SelectGroup>
          </SelectContent>
        </Select>
        <Select value={actor || "all"} onValueChange={(value) => setParams({ actor: !value || value === "all" ? null : value, page: null })}>
          <SelectTrigger size="sm" className="w-[200px]" aria-label="按操作人筛选">
            <UserIcon className="text-muted-foreground" aria-hidden="true" />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="all">全部操作人</SelectItem>
            </SelectGroup>
            {actorOptions.length > 0 ? <SelectSeparator /> : null}
            <SelectGroup>
              {actorOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <InputGroup className="h-8 w-full min-w-52 sm:w-[260px]">
          <InputGroupAddon align="inline-start">
            <SearchIcon aria-hidden="true" />
          </InputGroupAddon>
          <InputGroupInput
            value={search}
            placeholder="搜索对象或明细"
            aria-label="搜索对象或明细"
            onChange={(event) => {
              const value = event.target.value
              setSearch(value)
              if (searchTimer.current) clearTimeout(searchTimer.current)
              searchTimer.current = setTimeout(() => setParams({ q: value.trim() || null, page: null }), 350)
            }}
          />
          {search.length > 0 ? (
            <InputGroupAddon align="inline-end">
              <InputGroupButton
                size="icon-xs"
                aria-label="清除搜索"
                onClick={() => {
                  if (searchTimer.current) clearTimeout(searchTimer.current)
                  setSearch("")
                  setParams({ q: null, page: null })
                }}
              >
                <XIcon aria-hidden="true" />
              </InputGroupButton>
            </InputGroupAddon>
          ) : null}
        </InputGroup>
        {hasFilters ? (
          <Button size="sm" type="button" variant="ghost" onClick={clearFilters}>
            清除筛选
          </Button>
        ) : null}
      </div>

      {list.isError && !list.data ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : (
        <div className={cn("transition-opacity", list.isFetching && !list.isLoading && "opacity-70")}>
          <DataTable
            columns={columns}
            data={pageItems ?? []}
            loading={list.isLoading}
            onRowClick={handleOpen}
            page={page - 1}
            pageSize={size}
            total={total}
            onPageChange={(next) => setParams({ page: next > 0 ? next + 1 : null })}
            empty={
              <span className="flex flex-col items-center gap-2 py-4">
                <ScrollTextIcon className="size-5" aria-hidden="true" />
                {hasFilters ? "没有匹配的记录" : "还没有审计记录,修改配置、管理用户、编辑酶等操作发生后会记在这里。"}
              </span>
            }
          />
        </div>
      )}

      <AuditSheet
        entry={openEntry}
        user={openEntry ? usersByName.get(openEntry.actor) : undefined}
        open={sheetOpen}
        onOpenChange={(open) => {
          if (!open) setParams({ event: null })
        }}
        onFilterActor={(next) => setParams({ actor: next, page: null, event: null })}
        onFilterCategory={(next) => setParams({ category: next, page: null, event: null })}
      />
    </div>
  )
}
