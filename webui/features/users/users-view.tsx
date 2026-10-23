"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import type { ColumnDef } from "@tanstack/react-table"
import { PencilIcon, SearchIcon, ShieldIcon, Trash2Icon, UserPlusIcon, UsersIcon, XIcon } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { DataTable } from "@/components/data-table"
import { ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useUrlState } from "@/hooks/use-url-state"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useMe } from "@/lib/api/hooks"
import type { AdminUser } from "@/lib/api/types"
import { formatNumber, formatRelative, initials } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { CreateUserDialog } from "./create-user-dialog"
import {
  avatarSrc,
  isRoleFilter,
  isStatusFilter,
  protectionReason,
  ROLE_FILTER_OPTIONS,
  STATUS_TABS,
  userSearchBlob,
  type RoleFilter,
  type StatusFilter,
} from "./lib/users"
import { MemberSheet } from "./member-sheet"

const PAGE_SIZE = 10

/** 用户:谁能登录这个后台,各自是什么角色。筛选与打开的成员都在 URL 里,审计日志也能直接链到某个人。 */
export function UsersView() {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const me = useMe()
  const meId = me.data?.id
  const usersQuery = useQuery({ queryKey: qk.users, queryFn: adminApi.users, select: (data) => data.users })
  const users = useMemo(() => usersQuery.data ?? [], [usersQuery.data])

  const [params, setParams] = useUrlState()
  const query = params.get("q") ?? ""
  const roleParam = params.get("role")
  const statusParam = params.get("status")
  const roleFilter: RoleFilter = isRoleFilter(roleParam) ? roleParam : "all"
  const statusFilter: StatusFilter = isStatusFilter(statusParam) ? statusParam : "all"
  const openUsername = params.get("user")

  const [search, setSearch] = useState(query)
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [page, setPage] = useState(0)
  const [createOpen, setCreateOpen] = useState(false)

  // 抽屉关上的那一刻 URL 里已经没有这个人了,留着上一个才画得完关闭动画
  const [lastUsername, setLastUsername] = useState(openUsername)
  if (openUsername && openUsername !== lastUsername) setLastUsername(openUsername)
  const sheetUser = users.find((user) => user.username === (openUsername ?? lastUsername)) ?? null
  const sheetOpen = Boolean(openUsername) && Boolean(sheetUser)

  const activeAdmins = useMemo(() => users.filter((user) => user.role === "admin" && user.is_active).length, [users])

  useEffect(() => () => {
    if (searchTimer.current) clearTimeout(searchTimer.current)
  }, [])

  const normalizedQuery = query.trim().toLowerCase()
  const matching = useMemo(
    () =>
      users.filter(
        (user) => (roleFilter === "all" || user.role === roleFilter) && (!normalizedQuery || userSearchBlob(user).includes(normalizedQuery)),
      ),
    [users, roleFilter, normalizedQuery],
  )
  const statusCounts = {
    all: matching.length,
    active: matching.filter((user) => user.is_active).length,
    disabled: matching.filter((user) => !user.is_active).length,
  }
  // 新建的账号排在最前;排序在分页之前做,翻页时顺序才是对的
  const rows = useMemo(() => {
    const filtered = statusFilter === "all" ? matching : matching.filter((user) => (statusFilter === "active" ? user.is_active : !user.is_active))
    return [...filtered].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
  }, [matching, statusFilter])
  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE))
  const safePage = Math.min(page, pageCount - 1)
  const pageRows = rows.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE)

  const hasFilters = normalizedQuery.length > 0 || roleFilter !== "all" || statusFilter !== "all"
  const clearFilters = useCallback(() => {
    if (searchTimer.current) clearTimeout(searchTimer.current)
    setSearch("")
    setParams({ q: null, role: null, status: null })
    setPage(0)
  }, [setParams])

  const openUser = useCallback((user: AdminUser) => setParams({ user: user.username }), [setParams])

  const remove = useMutation({
    mutationFn: (user: AdminUser) => adminApi.deleteUser(user.id),
    onSuccess: (_result, user) => {
      queryClient.setQueryData<{ users: AdminUser[] }>(qk.users, (old) => (old ? { users: old.users.filter((item) => item.id !== user.id) } : old))
      void queryClient.invalidateQueries({ queryKey: qk.users })
      void queryClient.invalidateQueries({ queryKey: qk.audit() })
      toast.success("账号已删除", { description: `${user.display_name}(@${user.username})` })
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "删除失败"),
  })
  const removeUser = remove.mutate

  const requestDelete = useCallback(
    async (user: AdminUser) => {
      // 确认框在屏幕正中,抽屉会挡住它问的内容,先把抽屉收起来
      setParams({ user: null })
      const ok = await confirm({
        title: `删除 ${user.display_name}?`,
        description: `@${user.username} 会被永久删除,立即无法登录;TA 留在审计日志里的操作记录仍然保留。这一步不能撤销——只想暂时收回访问的话,请改为停用。`,
        confirmText: "删除账号",
        destructive: true,
      })
      if (ok) removeUser(user)
    },
    [confirm, removeUser, setParams],
  )

  const deleteBlockedReason = useCallback(
    (user: AdminUser) => {
      const guard = protectionReason(user, meId, activeAdmins)
      if (guard.self) return "不能删除自己"
      if (guard.lastAdmin) return "至少需要保留一个启用的管理员"
      return null
    },
    [activeAdmins, meId],
  )

  const columns = useMemo<ColumnDef<AdminUser, unknown>[]>(
    () => [
      {
        id: "user",
        header: "用户",
        cell: ({ row }) => {
          const user = row.original
          return (
            <div className="flex min-w-0 items-center gap-2.5">
              <Avatar className={cn("size-8 shrink-0", !user.is_active && "opacity-50 grayscale")}>
                <AvatarImage src={avatarSrc(user.avatar_url)} alt={user.display_name} />
                <AvatarFallback>{initials(user.display_name || user.username)}</AvatarFallback>
              </Avatar>
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-1.5">
                  <span className={cn("truncate text-sm font-medium", !user.is_active && "text-muted-foreground")}>{user.display_name}</span>
                  {user.id === meId ? <Badge variant="outline">你</Badge> : null}
                </div>
                <div className="truncate font-mono text-xs text-muted-foreground">@{user.username}</div>
              </div>
            </div>
          )
        },
      },
      {
        id: "role",
        header: "角色",
        cell: ({ row }) => {
          const role = labelOf.role(row.original.role)
          return (
            <ToneBadge tone={role.tone} className="gap-1">
              {row.original.role === "admin" ? <ShieldIcon aria-hidden="true" /> : null}
              {role.label}
            </ToneBadge>
          )
        },
      },
      {
        id: "status",
        header: "状态",
        cell: ({ row }) => (
          <span className="flex items-center gap-2 text-sm">
            <span className={cn("size-1.5 shrink-0 rounded-full", row.original.is_active ? "bg-emerald-500" : "bg-muted-foreground/40")} aria-hidden="true" />
            <span className={cn(!row.original.is_active && "text-muted-foreground")}>{row.original.is_active ? "启用" : "已停用"}</span>
          </span>
        ),
      },
      {
        id: "created_at",
        header: "创建时间",
        cell: ({ row }) => <span className="text-sm text-muted-foreground">{formatRelative(row.original.created_at)}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">操作</span>,
        cell: ({ row }) => {
          const user = row.original
          const blocked = deleteBlockedReason(user)
          return (
            <div className="flex justify-end gap-1">
              <Button
                type="button"
                size="icon-sm"
                variant="ghost"
                aria-label={`编辑 ${user.display_name}`}
                onClick={(event) => {
                  event.stopPropagation()
                  openUser(user)
                }}
              >
                <PencilIcon aria-hidden="true" />
              </Button>
              {blocked ? (
                // 禁用的按钮收不到指针事件,说明挂在外层 span 上
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="inline-flex" onClick={(event) => event.stopPropagation()}>
                      <Button type="button" size="icon-sm" variant="ghost" disabled aria-label={`不能删除 ${user.display_name}`}>
                        <Trash2Icon aria-hidden="true" />
                      </Button>
                    </span>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-64">{blocked}</TooltipContent>
                </Tooltip>
              ) : (
                <Button
                  type="button"
                  size="icon-sm"
                  variant="ghost"
                  className="hover:text-destructive"
                  aria-label={`删除 ${user.display_name}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    void requestDelete(user)
                  }}
                >
                  <Trash2Icon aria-hidden="true" />
                </Button>
              )}
            </div>
          )
        },
      },
    ],
    [deleteBlockedReason, meId, openUser, requestDelete],
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {usersQuery.isLoading
            ? "正在加载…"
            : `${formatNumber(users.length)} 个账号 · ${users.filter((u) => u.role === "admin").length} 名管理员${
                users.some((u) => !u.is_active) ? ` · ${users.filter((u) => !u.is_active).length} 个已停用` : ""
              }`}
        </p>
        <Button size="sm" type="button" onClick={() => setCreateOpen(true)}>
          <UserPlusIcon aria-hidden="true" /> 新建用户
        </Button>
      </div>

      <Tabs
        value={statusFilter}
        onValueChange={(value) => {
          setParams({ status: value === "all" ? null : value })
          setPage(0)
        }}
      >
        <TabsList>
          {STATUS_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value} className="gap-2">
              {tab.label}
              <span className="text-xs tabular-nums text-muted-foreground">{statusCounts[tab.value]}</span>
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex flex-wrap items-center gap-2">
        <InputGroup className="h-8 w-full min-w-52 sm:w-[260px]">
          <InputGroupAddon align="inline-start">
            <SearchIcon aria-hidden="true" />
          </InputGroupAddon>
          <InputGroupInput
            value={search}
            placeholder="搜索用户名或显示名"
            aria-label="搜索账号"
            onChange={(event) => {
              const value = event.target.value
              setSearch(value)
              if (searchTimer.current) clearTimeout(searchTimer.current)
              searchTimer.current = setTimeout(() => {
                setParams({ q: value.trim() || null })
                setPage(0)
              }, 250)
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
                  setParams({ q: null })
                  setPage(0)
                }}
              >
                <XIcon aria-hidden="true" />
              </InputGroupButton>
            </InputGroupAddon>
          ) : null}
        </InputGroup>
        <Select
          value={roleFilter}
          onValueChange={(value) => {
            setParams({ role: !value || value === "all" ? null : value })
            setPage(0)
          }}
        >
          <SelectTrigger size="sm" className="w-[132px]" aria-label="按角色筛选">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {ROLE_FILTER_OPTIONS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        {hasFilters ? (
          <Button type="button" size="sm" variant="ghost" onClick={clearFilters}>
            清除筛选
          </Button>
        ) : null}
      </div>

      {usersQuery.isError ? (
        <ErrorState error={usersQuery.error} onRetry={() => void usersQuery.refetch()} />
      ) : (
        <DataTable
          columns={columns}
          data={pageRows}
          loading={usersQuery.isLoading}
          onRowClick={openUser}
          page={safePage}
          pageSize={PAGE_SIZE}
          total={rows.length}
          onPageChange={setPage}
          empty={
            <span className="flex flex-col items-center gap-2 py-4">
              <UsersIcon className="size-5" aria-hidden="true" />
              {hasFilters ? "没有匹配的账号" : "还没有账号,新建一个把登录信息交给同事。"}
            </span>
          }
        />
      )}

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => {
          // 新账号带着什么角色都可能被当前筛选挡住:清掉筛选、回到第一页
          clearFilters()
        }}
      />
      <MemberSheet
        user={sheetUser}
        open={sheetOpen}
        onOpenChange={(open) => {
          if (!open) setParams({ user: null })
        }}
        meId={meId}
        activeAdmins={activeAdmins}
        onRequestDelete={requestDelete}
      />
      {confirmDialog}
    </div>
  )
}
