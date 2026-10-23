"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowRightIcon, ChevronRightIcon, SearchIcon, XIcon } from "lucide-react"
import Link from "next/link"
import { Fragment, useEffect, useMemo, useRef, useState } from "react"
import { toast } from "sonner"

import { Notice } from "@/components/notice"
import { SaveBar } from "@/components/save-bar"
import { EmptyState, ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { FieldGroup } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Kbd } from "@/components/ui/kbd"
import { Label } from "@/components/ui/label"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { useSearchParamState } from "@/hooks/use-url-state"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useModels, useProviders, useSettings } from "@/lib/api/hooks"
import type { SettingItem, SettingValue, SettingsSnapshot } from "@/lib/api/types"

import { draftFromValue, fallbackValue, matchesSetting, sameDraft, toSubmitValue, validateDraft, type DraftValue } from "../lib/settings"
import { SectionNav } from "./section-nav"
import { SettingRow } from "./setting-row"

/** 模型绑定有自己的页;这里列其余全部分组。 */
const EXCLUDED_GROUPS = new Set(["model"])

/**
 * 运行参数:后端参数注册表的全部可调项,按分组列出,控件由类型决定。
 * 加一个参数,后端 SPECS 里多一条,这页自动多一行。改动先留在页面上(每行标出已修改),
 * 底部保存条一次提交整批:后端按批原子写入,任何一项不合法整批都不落库,并指回出错的那一行。
 */
export function RuntimeSettingsView() {
  const [collapsed, setCollapsed] = useState<string[]>([])
  const settingsQuery = useSettings()
  const modelsQuery = useModels()
  const providersQuery = useProviders()
  const queryClient = useQueryClient()
  const [q, setQ] = useSearchParamState("q")
  const [advancedParam, setAdvancedParam] = useSearchParamState("advanced")
  const [draft, setDraft] = useState<Record<string, DraftValue>>({})
  const [resets, setResets] = useState<string[]>([])
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})

  const snapshot = settingsQuery.data
  const items = useMemo(() => (snapshot?.settings ?? []).filter((item) => !EXCLUDED_GROUPS.has(item.group)), [snapshot])
  const groups = useMemo(() => (snapshot?.groups ?? []).filter((group) => !EXCLUDED_GROUPS.has(group.key)), [snapshot])
  const byKey = useMemo(() => new Map(items.map((item) => [item.key, item])), [items])
  const models = useMemo(() => modelsQuery.data ?? [], [modelsQuery.data])
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data])

  const isDirty = (item: SettingItem) => item.key in draft && !sameDraft(item, draft[item.key], draftFromValue(item))
  const isReset = (item: SettingItem) => resets.includes(item.key)
  const valueOf = (item: SettingItem): DraftValue =>
    item.key in draft ? draft[item.key] : isReset(item) ? draftFromValue(item, fallbackValue(item).value) : draftFromValue(item)
  const errorOf = (item: SettingItem): string | undefined =>
    serverErrors[item.key] ?? (isDirty(item) ? validateDraft(item, draft[item.key]) : undefined)

  const dirtyItems = items.filter(isDirty)
  const resetItems = items.filter(isReset)
  const errorItems = items.filter((item) => Boolean(errorOf(item)))
  const changeCount = dirtyItems.length + resetItems.length

  const needle = q.trim().toLowerCase()
  const showAdvanced = advancedParam === "1"
  const visible = items.filter(
    (item) =>
      matchesSetting(item, needle) &&
      (showAdvanced || Boolean(needle) || !item.advanced || isDirty(item) || isReset(item) || Boolean(errorOf(item))),
  )
  const hiddenAdvanced = items.filter((item) => item.advanced && !visible.includes(item) && matchesSetting(item, needle))
  const advancedTotal = items.filter((item) => item.advanced).length
  const sections = groups
    .map((group) => ({ group, items: visible.filter((item) => item.group === group.key) }))
    .filter((section) => section.items.length > 0)

  function change(item: SettingItem, value: DraftValue) {
    setServerErrors((current) => {
      if (!(item.key in current)) return current
      const next = { ...current }
      delete next[item.key]
      return next
    })
    setResets((current) => current.filter((key) => key !== item.key))
    setDraft((current) => ({ ...current, [item.key]: value }))
  }

  function revert(item: SettingItem) {
    setDraft((current) => {
      const next = { ...current }
      delete next[item.key]
      return next
    })
    setServerErrors((current) => {
      const next = { ...current }
      delete next[item.key]
      return next
    })
  }

  function markReset(item: SettingItem) {
    revert(item)
    setResets((current) => (current.includes(item.key) ? current : [...current, item.key]))
  }

  function discardAll() {
    setDraft({})
    setResets([])
    setServerErrors({})
  }

  function scrollToRow(key: string) {
    // 出错的那一行可能在收起的分组里:先展开它所在的分组
    const group = byKey.get(key)?.group
    if (group) setCollapsed((current) => current.filter((item) => item !== group))
    requestAnimationFrame(() => document.getElementById(`row-${key}`)?.scrollIntoView({ behavior: "smooth", block: "center" }))
  }

  const save = useMutation({
    mutationFn: ({ values, reset }: { values: Record<string, SettingValue>; reset: string[] }) => adminApi.updateSettings(values, reset),
    onSuccess: (next: SettingsSnapshot) => {
      queryClient.setQueryData(qk.settings, next)
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.system })
      discardAll()
      const changed = next.changed ?? []
      const labels = changed.map((key) => byKey.get(key)?.label ?? key)
      if (changed.length === 0) toast.info("没有需要保存的变化", { description: "提交的值与现在生效的值相同。" })
      else toast.success(`已保存 ${changed.length} 项`, { description: labels.join("、") })
    },
    onError: (error) => {
      const field = error instanceof ApiError ? error.field : undefined
      const message = errorMessage(error)
      if (field) {
        setServerErrors({ [field]: message })
        scrollToRow(field)
      }
      toast.error("保存失败,整批都没有写入", {
        description: field ? `「${byKey.get(field)?.label ?? field}」:${message}` : message,
      })
    },
  })

  function handleSave() {
    if (changeCount === 0 || save.isPending) return
    const firstError = errorItems[0]
    if (firstError) {
      scrollToRow(firstError.key)
      toast.error(`还有 ${errorItems.length} 项取值不合法`, {
        description: errorItems.map((item) => `「${item.label}」${errorOf(item)}`).join(";"),
      })
      return
    }
    const values: Record<string, SettingValue> = {}
    for (const item of dirtyItems) values[item.key] = toSubmitValue(item, draft[item.key])
    save.mutate({ values, reset: resetItems.map((item) => item.key) })
  }

  // ⌘S / Ctrl+S 保存;有未保存改动时离开页面先确认
  const saveRef = useRef(handleSave)
  useEffect(() => {
    saveRef.current = handleSave
  })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault()
        saveRef.current()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [])
  useEffect(() => {
    if (changeCount === 0) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener("beforeunload", onBeforeUnload)
    return () => window.removeEventListener("beforeunload", onBeforeUnload)
  }, [changeCount])

  const restartKeys = snapshot?.restart_required ?? []

  return (
    <div className="flex flex-col gap-4">
      {restartKeys.length > 0 ? (
        <Notice
          tone="warning"
          title="有改动需要重启服务才生效"
          action={
            <Button size="sm" variant="outline" asChild>
              <Link href="/admin/system">
                系统状态 <ArrowRightIcon />
              </Link>
            </Button>
          }
        >
          {restartKeys.map((key) => snapshot?.settings.find((item) => item.key === key)?.label ?? key).join("、")}:运行中的进程仍在用启动时的值。
        </Notice>
      ) : null}

      {settingsQuery.isLoading ? (
        <div className="grid gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <Skeleton className="hidden h-80 lg:block" />
          <div className="flex flex-col gap-4">
            <Skeleton className="h-12" />
            <Skeleton className="h-72" />
            <Skeleton className="h-72" />
          </div>
        </div>
      ) : settingsQuery.error ? (
        <ErrorState error={settingsQuery.error} onRetry={() => void settingsQuery.refetch()} />
      ) : (
        <div className="grid items-start gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <aside className="sticky top-4 hidden lg:block">
            <SectionNav
              sections={sections.map((section) => ({
                key: section.group.key,
                label: section.group.label,
                count: section.items.length,
                dirty: section.items.filter((item) => isDirty(item) || isReset(item)).length,
                errors: section.items.filter((item) => Boolean(errorOf(item))).length,
              }))}
            />
          </aside>
          <div className="flex min-w-0 flex-col gap-4">
            <Card>
              <div className="flex flex-col gap-3 px-4 sm:flex-row sm:items-center sm:justify-between">
                <InputGroup className="w-full sm:max-w-sm">
                  <InputGroupAddon>
                    <SearchIcon />
                  </InputGroupAddon>
                  <InputGroupInput aria-label="搜索参数" placeholder="搜索名称、说明或键名" value={q} onChange={(event) => setQ(event.target.value)} />
                  {q ? (
                    <InputGroupAddon align="inline-end">
                      <InputGroupButton size="icon-xs" aria-label="清除搜索" onClick={() => setQ("")}>
                        <XIcon />
                      </InputGroupButton>
                    </InputGroupAddon>
                  ) : null}
                </InputGroup>
                <div className="flex flex-wrap items-center gap-3">
                  <span className="hidden text-xs text-muted-foreground xl:inline">
                    <Kbd>⌘</Kbd> <Kbd>S</Kbd> 保存
                  </span>
                  <Label className="flex cursor-pointer items-center gap-2 font-normal">
                    <Switch size="sm" checked={showAdvanced} onCheckedChange={(checked) => setAdvancedParam(checked ? "1" : "")} />
                    显示高级参数({advancedTotal})
                  </Label>
                </div>
              </div>
            </Card>

            {sections.length === 0 ? (
              <Card>
                <EmptyState
                  icon={SearchIcon}
                  title={`没有匹配「${q}」的参数`}
                  description="按名称或键名(如 judge.pool)搜索。模型绑定在单独的页面。"
                  action={
                    <Button size="sm" variant="outline" onClick={() => setQ("")}>
                      清除搜索
                    </Button>
                  }
                />
              </Card>
            ) : (
              sections.map(({ group, items: rows }) => (
                <Card key={group.key} id={`group-${group.key}`} data-group={group.key} className="scroll-mt-4 gap-0 py-0">
                  <Collapsible
                    open={!collapsed.includes(group.key)}
                    onOpenChange={(open) => setCollapsed((current) => (open ? current.filter((key) => key !== group.key) : [...current, group.key]))}
                  >
                    <CardHeader className="flex-row items-center justify-between gap-3 border-b py-3">
                      <CollapsibleTrigger className="group/trigger flex min-w-0 flex-1 items-center gap-2 text-start outline-none">
                        <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]/trigger:rotate-90" aria-hidden="true" />
                        <div className="flex min-w-0 flex-col gap-px">
                          <CardTitle className="text-sm">{group.label}</CardTitle>
                          <CardDescription className="text-xs">{group.description}</CardDescription>
                        </div>
                      </CollapsibleTrigger>
                      {group.key === "gateway" ? (
                        <Button size="xs" variant="ghost" className="shrink-0" asChild>
                          <Link href="/models/gateway">
                            可视化编辑 <ArrowRightIcon />
                          </Link>
                        </Button>
                      ) : null}
                    </CardHeader>
                    <CollapsibleContent>
                      {/* FieldGroup 提供 @container/field-group:参数行按面板宽度决定左右排还是上下排 */}
                      <FieldGroup className="gap-0">
                        {rows.map((item, index) => (
                          <Fragment key={item.key}>
                            {index > 0 ? <Separator /> : null}
                            <SettingRow
                              item={item}
                              value={valueOf(item)}
                              dirty={isDirty(item)}
                              pendingReset={isReset(item)}
                              error={errorOf(item)}
                              models={models}
                              providers={providers}
                              onChange={(value) => change(item, value)}
                              onRevert={() => revert(item)}
                              onReset={() => markReset(item)}
                              onCancelReset={() => setResets((current) => current.filter((key) => key !== item.key))}
                            />
                          </Fragment>
                        ))}
                      </FieldGroup>
                    </CollapsibleContent>
                  </Collapsible>
                </Card>
              ))
            )}

            {!showAdvanced && hiddenAdvanced.length > 0 ? (
              <button
                type="button"
                onClick={() => setAdvancedParam("1")}
                className="rounded-xl border border-dashed px-4 py-3 text-sm text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
              >
                还有 {hiddenAdvanced.length} 项高级参数没有显示——显示全部
              </button>
            ) : null}

            <SaveBar
              count={changeCount}
              saving={save.isPending}
              onDiscard={discardAll}
              onSave={handleSave}
              note={
                errorItems.length > 0 ? (
                  <span className="text-destructive">{errorItems.length} 项取值不合法</span>
                ) : resetItems.length > 0 ? (
                  `其中 ${resetItems.length} 项恢复默认`
                ) : (
                  dirtyItems.slice(0, 3).map((item) => item.label).join("、") + (dirtyItems.length > 3 ? " 等" : "")
                )
              }
            />
          </div>
        </div>
      )}
    </div>
  )
}
