"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowRightIcon, CircleAlertIcon, RotateCcwIcon, SaveIcon, TriangleAlertIcon } from "lucide-react"
import Link from "next/link"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useModels, useProviders, useRoles, useSettings } from "@/lib/api/hooks"
import type { ModelEntry, Provider, RoleStatus } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { unusableReason } from "./lib/registry"

/** Radix Select 不接受空字符串作为选项值;「不绑定」用一个占位值表示,提交时换回空串。 */
const UNBOUND = "__unbound__"

/** 未单独指定时沿用引擎 LLM 的两个角色(后端 source=inherit)。 */
const INHERITS_ENGINE = new Set(["model.query_expand", "model.judge_llm"])

/** 「不绑定」时这个环节实际用什么。 */
function fallbackLabel(role: RoleStatus, engine?: RoleStatus) {
  if (INHERITS_ENGINE.has(role.key)) return engine?.model ? `沿用引擎 LLM(${engine.model})` : "沿用引擎 LLM"
  return role.source === "env" && role.model ? `沿用环境变量(${role.model})` : "不绑定,沿用环境变量"
}

/**
 * 模型绑定:每个环节现在交给谁、能不能用。一行一个环节,改动先留在页面上,
 * 卡片标题栏出现「保存」;只提交改过的角色。下拉只列类型相符的模型,停用的列出来但不能选并说明原因。
 */
export function BindingsView() {
  const rolesQuery = useRoles()
  const modelsQuery = useModels()
  const providersQuery = useProviders()
  const settingsQuery = useSettings()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [embeddingChanged, setEmbeddingChanged] = useState(false)

  const roles = useMemo(() => rolesQuery.data?.roles ?? [], [rolesQuery.data])
  const models = useMemo(() => modelsQuery.data ?? [], [modelsQuery.data])
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data])
  const restartRequired = rolesQuery.data?.restart_required ?? []
  const engine = roles.find((r) => r.key === "model.llm")

  const valueOf = (role: RoleStatus) => (role.key in draft ? draft[role.key] : role.model_id)
  const dirtyKeys = roles.filter((role) => role.key in draft && draft[role.key] !== role.model_id).map((r) => r.key)

  const save = useMutation({
    mutationFn: (bindings: Record<string, string>) => adminApi.bindRoles(bindings),
    onSuccess: (response, bindings) => {
      queryClient.setQueryData(qk.roles, response)
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.settings })
      void queryClient.invalidateQueries({ queryKey: qk.system })
      setDraft({})
      const labels = Object.keys(bindings).map((key) => roles.find((r) => r.key === key)?.label ?? key)
      toast.success(`已保存 ${labels.length} 个环节`, { description: labels.join("、") })
      if ("model.embedding" in bindings) setEmbeddingChanged(true)
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "保存失败"),
  })

  function handleSave() {
    const bindings = Object.fromEntries(dirtyKeys.map((key) => [key, draft[key]]))
    if (Object.keys(bindings).length === 0) return
    save.mutate(bindings)
  }

  const settingLabel = (key: string) => settingsQuery.data?.settings.find((s) => s.key === key)?.label ?? key
  const loading = rolesQuery.isLoading || modelsQuery.isLoading

  return (
    <div className="flex flex-col gap-4">
      {restartRequired.length > 0 ? (
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
          {restartRequired.map(settingLabel).join("、")}
          {restartRequired.includes("model.embedding") ? ":向量维度变了,运行中的向量库仍按旧维度建表。" : "。"}
        </Notice>
      ) : null}
      {embeddingChanged ? (
        <Notice
          tone="info"
          title="换了向量模型"
          action={
            <Button size="sm" variant="outline" asChild>
              <Link href="/admin/system">
                去重建索引 <ArrowRightIcon />
              </Link>
            </Button>
          }
        >
          已有记忆的向量仍是旧模型算的,和新模型算的查询向量不在一个空间里。到「系统状态」页重建向量索引后召回才准确。
        </Notice>
      ) : null}

      <Card className="gap-0 py-0">
        <CardHeader className="flex min-h-12 items-center border-b py-3">
          <CardTitle className="text-sm">各环节在用的模型</CardTitle>
          {dirtyKeys.length > 0 ? (
            <CardAction className="flex items-center gap-2">
              <Button size="sm" variant="ghost" disabled={save.isPending} onClick={() => setDraft({})}>
                <RotateCcwIcon /> 放弃
              </Button>
              <Button size="sm" disabled={save.isPending} onClick={handleSave}>
                {save.isPending ? <Spinner /> : <SaveIcon />}
                保存({dirtyKeys.length})
              </Button>
            </CardAction>
          ) : null}
        </CardHeader>
        {loading ? (
          <div className="flex flex-col gap-2 p-3">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
        ) : rolesQuery.error ? (
          <ErrorState error={rolesQuery.error} onRetry={() => void rolesQuery.refetch()} />
        ) : (
          <ul className="flex flex-col divide-y">
            {roles.map((role) => (
              <RoleRow
                key={role.key}
                role={role}
                value={valueOf(role)}
                dirty={dirtyKeys.includes(role.key)}
                models={models}
                providers={providers}
                engine={engine}
                onChange={(value) =>
                  setDraft((current) => {
                    const next = { ...current }
                    if (value === role.model_id) delete next[role.key]
                    else next[role.key] = value
                    return next
                  })
                }
              />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

function RoleRow({
  role,
  value,
  dirty,
  models,
  providers,
  engine,
  onChange,
}: {
  role: RoleStatus
  /** 当前选择(草稿优先),"" 表示不绑定 */
  value: string
  dirty: boolean
  models: ModelEntry[]
  providers: Provider[]
  engine?: RoleStatus
  onChange: (value: string) => void
}) {
  const candidates = useMemo(() => models.filter((m) => role.kinds.includes(m.kind)), [models, role.kinds])
  const grouped = useMemo(() => {
    const byProvider = new Map<string, ModelEntry[]>()
    for (const m of candidates) {
      const list = byProvider.get(m.provider_id) ?? []
      list.push(m)
      byProvider.set(m.provider_id, list)
    }
    return [...byProvider.entries()].map(([providerId, list]) => ({
      provider: providers.find((p) => p.id === providerId),
      providerId,
      models: list,
    }))
  }, [candidates, providers])
  const selected = value ? models.find((m) => m.id === value) : undefined
  const selectedIssue = value ? unusableReason(selected, providers) : undefined
  const selectId = `role-${role.key}`
  const fallback = fallbackLabel(role, engine)
  const kinds = role.kinds.map((k) => labelOf.kind(k).label).join(" / ")

  return (
    <li data-row-id={role.key} className={cn("flex flex-col gap-2 px-6 py-3 transition-colors", dirty && "bg-primary/[0.03]")}>
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:gap-4">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <label htmlFor={selectId} className="text-sm font-medium text-foreground">
            {role.label}
          </label>
          <span className="line-clamp-1 text-xs text-muted-foreground" title={role.help}>
            {role.help}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Select value={value || UNBOUND} onValueChange={(next) => onChange(next === UNBOUND ? "" : next)}>
            <SelectTrigger id={selectId} size="sm" className="w-full md:w-80" aria-invalid={Boolean(role.error || selectedIssue) || undefined}>
              <SelectValue>
                {value ? (
                  <span className="flex min-w-0 items-center gap-2">
                    <span className="truncate font-mono">{value}</span>
                    {selected ? <span className="truncate text-xs text-muted-foreground">{selected.provider_name}</span> : null}
                  </span>
                ) : (
                  <span className="truncate text-muted-foreground">{fallback}</span>
                )}
              </SelectValue>
            </SelectTrigger>
            <SelectContent className="min-w-80">
              <SelectGroup>
                <SelectItem value={UNBOUND}>{fallback}</SelectItem>
              </SelectGroup>
              {value && !selected ? (
                <SelectGroup>
                  <SelectItem value={value} disabled>
                    <span className="font-mono">{value}</span>
                    <span className="text-xs text-destructive">不存在</span>
                  </SelectItem>
                </SelectGroup>
              ) : null}
              {grouped.length === 0 ? (
                <SelectGroup>
                  <SelectSeparator />
                  <SelectLabel>还没有{kinds}模型,先到「服务商与模型」注册</SelectLabel>
                </SelectGroup>
              ) : (
                grouped.map((group) => (
                  <SelectGroup key={group.providerId}>
                    <SelectSeparator />
                    <SelectLabel>{group.provider?.name ?? group.providerId}</SelectLabel>
                    {group.models.map((m) => {
                      const issue = unusableReason(m, providers)
                      return (
                        <SelectItem key={m.id} value={m.id} disabled={Boolean(issue) && m.id !== value}>
                          <span className="flex min-w-0 flex-1 items-center gap-2">
                            <span className="truncate font-mono">{m.id}</span>
                            <span className="truncate text-xs text-muted-foreground">{m.model}</span>
                            {issue ? <span className="ms-auto text-xs text-muted-foreground">{issue}</span> : null}
                          </span>
                        </SelectItem>
                      )
                    })}
                  </SelectGroup>
                ))
              )}
            </SelectContent>
          </Select>
          <RoleState role={role} dirty={dirty} />
        </div>
      </div>
      {role.error ? (
        <p className="flex items-start gap-2 text-xs text-destructive">
          <CircleAlertIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>
            {role.error}(绑定的是 <span className="font-mono">{role.model_id}</span>)。在「服务商与模型」里启用它,或改绑一个可用的模型。
          </span>
        </p>
      ) : selectedIssue && dirty ? (
        <p className="flex items-start gap-2 text-xs text-amber-700 dark:text-amber-400">
          <TriangleAlertIcon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{selectedIssue}:保存后这个环节会显示为绑定异常。</span>
        </p>
      ) : null}
    </li>
  )
}

/** 只在需要注意时说话:正常绑定、正常沿用都不出徽章。宽度固定,下拉在各行对齐。 */
function RoleState({ role, dirty }: { role: RoleStatus; dirty: boolean }) {
  let badge: React.ReactNode = null
  if (dirty) badge = <Badge variant="secondary">未保存</Badge>
  else if (role.error) badge = <Badge variant="destructive">不可用</Badge>
  else if (role.source === "none") badge = <Badge variant="destructive">未配置</Badge>
  else if (role.source === "env") badge = <Badge variant="outline">环境变量</Badge>
  return <span className="flex shrink-0 justify-end md:w-20">{badge}</span>
}
