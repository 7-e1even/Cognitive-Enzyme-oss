"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import {
  CopyIcon,
  EllipsisVerticalIcon,
  EyeIcon,
  EyeOffIcon,
  FlaskConicalIcon,
  KeyRoundIcon,
  PauseIcon,
  PencilIcon,
  PlayIcon,
  PlugZapIcon,
  PlusIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react"
import Link from "next/link"
import { useState } from "react"
import { toast } from "sonner"

import { Spinner } from "@/components/spinner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Switch } from "@/components/ui/switch"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { ModelEntry, Protocol, Provider, ProviderPatch } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { providerState, ProviderLogo, ProviderStateBadge } from "./bits"
import { probeKey, useProviderProbe } from "./lib/queries"
import { KINDS, paramsSummary, unusableReason, usageHref, usageLabel } from "./lib/registry"
import { ProbeResultPanel } from "./probe-result"

export type ProviderAction = "edit" | "add-model" | "delete"
export type ModelAction = "edit" | "test" | "delete" | "copy"

/** 对话请求实际发往的地址,和后端拼法一致;System One 协议不预览。 */
function requestPreview(protocol: Protocol, baseUrl: string): string | null {
  const base = baseUrl.trim().replace(/\/+$/, "")
  if (!/^https?:\/\/\S+$/.test(base)) return null
  if (protocol === "openai") return `${base}/chat/completions`
  if (protocol === "anthropic") {
    if (base.endsWith("/messages")) return base
    return base.endsWith("/v1") ? `${base}/messages` : `${base}/v1/messages`
  }
  return null
}

/**
 * 右栏:一家服务商的设置。从上到下:名称与启用开关、API 密钥(只写)、API 地址(带请求地址预览)、
 * 检测结果(列出服务商提供的模型,点一下注册)、按类型分组的模型。协议、请求头、备注在「编辑全部」里。
 * 父组件以服务商 id 作 key 挂载,换一家就是一份新的草稿。
 */
export function ProviderPane({
  provider,
  models,
  providers,
  onAction,
  onToggle,
  onModelAction,
  onModelToggle,
  onPickProbeModel,
}: {
  provider: Provider
  models: ModelEntry[]
  providers: Provider[]
  onAction: (action: ProviderAction, provider: Provider) => void
  onToggle: (provider: Provider, enabled: boolean) => void
  onModelAction: (action: ModelAction, model: ModelEntry) => void
  onModelToggle: (model: ModelEntry, enabled: boolean) => void
  onPickProbeModel: (provider: Provider, model: string) => void
}) {
  const queryClient = useQueryClient()
  const probe = useProviderProbe(provider.id)
  const [keyDraft, setKeyDraft] = useState("")
  const [showKey, setShowKey] = useState(false)
  const [urlDraft, setUrlDraft] = useState(provider.base_url)
  const urlDirty = urlDraft.trim() !== provider.base_url
  const preview = requestPreview(provider.protocol, urlDraft)

  const patch = useMutation({
    mutationFn: (data: ProviderPatch) => adminApi.updateProvider(provider.id, data),
    onSuccess: (_saved, data) => {
      void queryClient.invalidateQueries({ queryKey: qk.providers })
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
      // 连接变了,上一次的检测结果不再作数
      void queryClient.resetQueries({ queryKey: probeKey(provider.id), exact: true })
      if ("api_key" in data) {
        setKeyDraft("")
        setShowKey(false)
        toast.success("已更新密钥")
      } else {
        toast.success("已更新 API 地址")
      }
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "保存失败"),
  })

  function saveUrl() {
    const value = urlDraft.trim()
    if (!/^https?:\/\/\S+$/.test(value)) {
      toast.error("API 地址要以 http:// 或 https:// 开头")
      return
    }
    patch.mutate({ base_url: value })
  }

  const registered = new Set(models.map((m) => m.model))
  const groups = KINDS.map((kind) => ({ kind, list: models.filter((m) => m.kind === kind) })).filter(
    (g) => g.list.length > 0,
  )
  const keyId = `provider-${provider.id}-key`
  const urlId = `provider-${provider.id}-url`

  return (
    <div className="flex w-full max-w-3xl min-w-0 flex-col gap-6 p-4 md:p-6">
      <div className="flex flex-wrap items-center gap-3">
        <ProviderLogo name={provider.name} />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex min-w-0 items-center gap-2">
            <h3 className="min-w-0 truncate text-base font-semibold">{provider.name}</h3>
            <ProviderStateBadge state={providerState(provider, probe.data)} />
            {probe.data?.ok ? (
              <Badge className="hidden sm:inline-flex" variant="outline">
                {Math.round(probe.data.latency_ms)} ms
              </Badge>
            ) : null}
          </div>
          <p className="truncate text-sm text-muted-foreground">
            <span className="font-mono text-[13px]">{provider.id}</span>
            <span aria-hidden="true"> / </span>
            {labelOf.protocol(provider.protocol).label}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="outline" disabled={probe.isFetching} onClick={() => void probe.refetch()}>
            {probe.isFetching ? <Spinner /> : <PlugZapIcon />}
            检测
          </Button>
          <Switch
            checked={provider.enabled}
            aria-label={provider.enabled ? `停用 ${provider.name}` : `启用 ${provider.name}`}
            onCheckedChange={(checked) => onToggle(provider, checked)}
          />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button type="button" size="icon-xs" variant="outline" aria-label={`${provider.name} 的操作`}>
                <EllipsisVerticalIcon aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52">
              <DropdownMenuItem onClick={() => void probe.refetch()}>
                <PlugZapIcon aria-hidden="true" />
                检测连接
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => document.getElementById(keyId)?.focus()}>
                <KeyRoundIcon aria-hidden="true" />
                更换密钥
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => onAction("edit", provider)}>
                <PencilIcon aria-hidden="true" />
                编辑全部(协议、请求头、备注)
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={() => onToggle(provider, !provider.enabled)}>
                {provider.enabled ? <PauseIcon aria-hidden="true" /> : <PlayIcon aria-hidden="true" />}
                {provider.enabled ? "停用服务商" : "启用服务商"}
              </DropdownMenuItem>
              <DropdownMenuItem variant="destructive" onClick={() => onAction("delete", provider)}>
                <Trash2Icon aria-hidden="true" />
                删除服务商
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <Field className="gap-2">
        <FieldLabel htmlFor={keyId}>API 密钥</FieldLabel>
        <div className="flex items-center gap-2">
          <InputGroup className="flex-1">
            <InputGroupInput
              id={keyId}
              type={showKey ? "text" : "password"}
              autoComplete="off"
              className="font-mono"
              placeholder={provider.api_key_set ? `已设置 ${provider.api_key_hint},输入新密钥可替换` : "未设置"}
              value={keyDraft}
              onChange={(event) => setKeyDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && keyDraft.trim()) patch.mutate({ api_key: keyDraft.trim() })
              }}
            />
            {keyDraft ? (
              <InputGroupAddon align="inline-end">
                <InputGroupButton size="icon-xs" aria-label={showKey ? "隐藏密钥" : "显示密钥"} onClick={() => setShowKey((v) => !v)}>
                  {showKey ? <EyeOffIcon /> : <EyeIcon />}
                </InputGroupButton>
              </InputGroupAddon>
            ) : null}
          </InputGroup>
          {keyDraft.trim() ? (
            <Button size="sm" disabled={patch.isPending} onClick={() => patch.mutate({ api_key: keyDraft.trim() })}>
              {patch.isPending ? <Spinner /> : null}
              保存
            </Button>
          ) : null}
        </div>
        {provider.enabled && !provider.api_key_set ? (
          <FieldDescription className="text-xs text-amber-700 dark:text-amber-400">没有密钥时只有本机服务能连通。</FieldDescription>
        ) : null}
      </Field>

      <Field className="gap-2">
        <FieldLabel htmlFor={urlId}>API 地址</FieldLabel>
        <div className="flex items-center gap-2">
          <Input
            id={urlId}
            className="flex-1 font-mono"
            value={urlDraft}
            onChange={(event) => setUrlDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && urlDirty) saveUrl()
              if (event.key === "Escape") setUrlDraft(provider.base_url)
            }}
          />
          {urlDirty ? (
            <>
              <Button size="sm" variant="ghost" onClick={() => setUrlDraft(provider.base_url)}>
                还原
              </Button>
              <Button size="sm" disabled={patch.isPending} onClick={saveUrl}>
                保存
              </Button>
            </>
          ) : null}
        </div>
        {preview ? (
          <FieldDescription className="truncate text-xs">
            预览:<span className="font-mono">{preview}</span>
          </FieldDescription>
        ) : null}
      </Field>

      {probe.data ? (
        <ProbeResultPanel
          result={probe.data}
          testedAt={probe.dataUpdatedAt}
          registered={registered}
          onPickModel={(name) => onPickProbeModel(provider, name)}
          onDismiss={() => void queryClient.resetQueries({ queryKey: probeKey(provider.id), exact: true })}
        />
      ) : null}

      <section className="flex flex-col gap-3" aria-labelledby={`provider-${provider.id}-models`}>
        <div className="flex items-center justify-between gap-3">
          <h4 id={`provider-${provider.id}-models`} className="flex items-center gap-2 text-sm font-medium">
            模型
            <Badge variant="outline">{models.length}</Badge>
          </h4>
          <Button size="sm" variant="outline" onClick={() => onAction("add-model", provider)}>
            <PlusIcon /> 添加模型
          </Button>
        </div>
        {models.length === 0 ? (
          <p className="rounded-xl border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
            还没有模型。点上面的「检测」列出服务商提供的模型,点一下就能注册;也可以手动添加。
          </p>
        ) : (
          groups.map(({ kind, list }) => (
            <div key={kind} className="flex flex-col gap-1.5">
              <span className="px-1 text-xs text-muted-foreground">{labelOf.kind(kind).label}</span>
              <ul className="divide-y rounded-xl border">
                {list.map((model) => (
                  <ModelRow key={model.id} model={model} providers={providers} onAction={onModelAction} onToggle={onModelToggle} />
                ))}
              </ul>
            </div>
          ))
        )}
      </section>
    </div>
  )
}

function ModelRow({
  model,
  providers,
  onAction,
  onToggle,
}: {
  model: ModelEntry
  providers: Provider[]
  onAction: (action: ModelAction, model: ModelEntry) => void
  onToggle: (model: ModelEntry, enabled: boolean) => void
}) {
  const summary = paramsSummary(model.kind, model.params ?? {})
  // 模型自己启用着、却因为服务商停用而用不了:这一层原因列表上看不出来,单独提示
  const reason = model.enabled ? unusableReason(model, providers) : undefined

  return (
    <li data-row-id={`model:${model.id}`} className="flex items-center gap-3 px-3 py-2.5">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            className={cn(
              "min-w-0 cursor-pointer truncate rounded-sm text-start font-mono text-sm transition-colors outline-none hover:text-primary focus-visible:ring-3 focus-visible:ring-ring/50",
              !model.enabled && "text-muted-foreground",
            )}
            title={`编辑 ${model.id}`}
            onClick={() => onAction("edit", model)}
          >
            {model.id}
          </button>
          {!model.enabled ? (
            <Badge variant="outline">已停用</Badge>
          ) : reason ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" className="gap-1 text-amber-700 dark:text-amber-400">
                  <TriangleAlertIcon aria-hidden="true" />
                  不可用
                </Badge>
              </TooltipTrigger>
              <TooltipContent>{reason}</TooltipContent>
            </Tooltip>
          ) : null}
        </div>
        <p className="min-w-0 truncate text-sm text-muted-foreground">
          {model.model !== model.id ? (
            <>
              <span className="font-mono text-[13px]">{model.model}</span>
              <span aria-hidden="true"> / </span>
            </>
          ) : null}
          <span>{summary.length > 0 ? summary.join(" · ") : "默认参数"}</span>
          {model.used_by.length > 0 ? (
            <>
              <span aria-hidden="true"> / </span>
              用于{" "}
              {model.used_by.map((key, i) => (
                <Link key={key} href={usageHref(key)} className="text-foreground underline-offset-3 hover:text-primary hover:underline">
                  {usageLabel(key)}
                  {i < model.used_by.length - 1 ? "、" : ""}
                </Link>
              ))}
            </>
          ) : null}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button type="button" size="sm" variant="outline" className="hidden md:inline-flex" onClick={() => onAction("test", model)}>
          <FlaskConicalIcon /> 测试
        </Button>
        <Switch
          size="sm"
          checked={model.enabled}
          aria-label={model.enabled ? `停用 ${model.id}` : `启用 ${model.id}`}
          onCheckedChange={(checked) => onToggle(model, checked)}
        />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" size="icon-xs" variant="outline" aria-label={`${model.id} 的操作`}>
              <EllipsisVerticalIcon aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuGroup>
              <DropdownMenuItem onClick={() => onAction("test", model)}>
                <FlaskConicalIcon aria-hidden="true" />
                测试模型
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => onAction("edit", model)}>
                <PencilIcon aria-hidden="true" />
                编辑模型
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => onAction("copy", model)}>
                <CopyIcon aria-hidden="true" />
                复制注册名
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem variant="destructive" onClick={() => onAction("delete", model)}>
                <Trash2Icon aria-hidden="true" />
                删除模型
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </li>
  )
}
