"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { EyeIcon, EyeOffIcon, PlusIcon, RotateCcwIcon, Trash2Icon } from "lucide-react"
import { useMemo, useState } from "react"
import { Controller, useFieldArray, useForm, useWatch } from "react-hook-form"
import { z } from "zod"

import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
  FieldTitle,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { ModelEntry, Protocol, Provider, ProviderPatch } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { KindBadge, ProviderLogo } from "./bits"
import { PROVIDER_PRESETS, presetByKey, type PresetModel, type ProviderPreset } from "./lib/presets"
import { probeKey } from "./lib/queries"
import { ID_PATTERN, ID_RULE, kindsFor, paramsSummary, PROTOCOLS, uniqueId } from "./lib/registry"
import { SHEET_PANEL } from "./sheet-styles"

/**
 * 新建服务商,或修改一个。新建时先挑预设:协议、接口地址、常用请求头和建议模型一次带齐,剩下的只有密钥。
 * 修改时密钥是只写的:看得到末四位,留空保持不变,要清除得明说。
 */
const headerRow = z.object({ key: z.string(), value: z.string() })

const providerSchema = z.object({
  id: z.string().trim().min(1, "请填写服务商 ID").regex(ID_PATTERN, `服务商 ID ${ID_RULE}`),
  name: z.string().trim().max(80, "名称不超过 80 个字符"),
  protocol: z.enum(["openai", "anthropic", "system_one"]),
  base_url: z
    .string()
    .trim()
    .min(1, "请填写接口地址")
    .regex(/^https?:\/\/\S+$/, "接口地址需以 http:// 或 https:// 开头,且不含空格"),
  api_key: z.string(),
  clear_api_key: z.boolean(),
  headers: z.array(headerRow).superRefine((rows, ctx) => {
    const seen = new Set<string>()
    rows.forEach((row, index) => {
      const key = row.key.trim()
      if (!key && row.value.trim()) {
        ctx.addIssue({ code: "custom", path: [index, "key"], message: "请求头名不能为空" })
      }
      if (key && seen.has(key.toLowerCase())) {
        ctx.addIssue({ code: "custom", path: [index, "key"], message: `请求头 ${key} 重复` })
      }
      if (key) seen.add(key.toLowerCase())
    })
  }),
  enabled: z.boolean(),
  note: z.string().trim().max(500, "备注不超过 500 个字符"),
})
type ProviderFormValues = z.infer<typeof providerSchema>

function blankValues(): ProviderFormValues {
  return { id: "", name: "", protocol: "openai", base_url: "", api_key: "", clear_api_key: false, headers: [], enabled: true, note: "" }
}

function toValues(provider: Provider): ProviderFormValues {
  return {
    id: provider.id,
    name: provider.name,
    protocol: provider.protocol,
    base_url: provider.base_url,
    api_key: "",
    clear_api_key: false,
    headers: Object.entries(provider.headers ?? {}).map(([key, value]) => ({ key, value })),
    enabled: provider.enabled,
    note: provider.note,
  }
}

function toRecord(rows: { key: string; value: string }[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (key) out[key] = row.value
  }
  return out
}

export interface ProviderSaved {
  provider: Provider
  created: boolean
  models: string[]
  modelErrors: string[]
}

export function ProviderFormSheet({
  open,
  onOpenChange,
  provider,
  providers,
  models,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 有值即编辑,没有就是新建 */
  provider: Provider | null
  providers: Provider[]
  models: ModelEntry[]
  onSaved: (result: ProviderSaved) => void
}) {
  const queryClient = useQueryClient()
  const editing = Boolean(provider)
  const form = useForm<ProviderFormValues>({
    resolver: zodResolver(providerSchema),
    defaultValues: provider ? toValues(provider) : blankValues(),
    mode: "onTouched",
  })
  const errors = form.formState.errors
  const headers = useFieldArray({ control: form.control, name: "headers" })
  const [presetKey, setPresetKey] = useState<string | null>(null)
  const [variantKey, setVariantKey] = useState<string | null>(null)
  const [pickingPreset, setPickingPreset] = useState(true)
  const [suggested, setSuggested] = useState<Set<string>>(new Set())
  const [revealKey, setRevealKey] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  // 预设最后一次写进去的名称与 ID:用户没改过才跟着换预设走,改过就不动
  const [applied, setApplied] = useState<{ name: string; id: string }>({ name: "", id: "" })
  const [seen, setSeen] = useState({ open, provider })

  // 重新打开时从当前要编辑的记录(或空白)开始,上一次没提交的半截内容不留下来
  if (seen.open !== open || seen.provider !== provider) {
    setSeen({ open, provider })
    if (open) {
      form.reset(provider ? toValues(provider) : blankValues())
      setPresetKey(null)
      setVariantKey(null)
      setPickingPreset(!provider)
      setSuggested(new Set())
      setRevealKey(false)
      setFormError(null)
      setApplied({ name: "", id: "" })
    }
  }

  const protocol = useWatch({ control: form.control, name: "protocol" })
  const clearKey = useWatch({ control: form.control, name: "clear_api_key" })
  const preset = presetByKey(presetKey)
  const variant = preset?.variants.find((v) => v.key === variantKey) ?? preset?.variants[0]
  const takenProviderIds = useMemo(() => providers.map((p) => p.id), [providers])
  const takenModelIds = useMemo(() => models.map((m) => m.id), [models])

  // 修改协议时,已挂的模型里协议不再支持的那些:后端会整笔拒绝,这里先说
  const incompatible = useMemo(() => {
    if (!provider) return []
    const allowed = kindsFor(protocol)
    return models.filter((m) => m.provider_id === provider.id && !allowed.includes(m.kind))
  }, [models, protocol, provider])

  const suggestionList = useMemo(() => {
    if (!preset?.models) return []
    const allowed = kindsFor(protocol)
    const taken = new Set(takenModelIds)
    return preset.models
      .filter((m) => allowed.includes(m.kind))
      .map((m) => {
        const id = uniqueId(m.id, taken)
        taken.add(id)
        return { ...m, finalId: id }
      })
  }, [preset, protocol, takenModelIds])

  function applyPreset(next: ProviderPreset, nextVariantKey?: string) {
    const nextVariant = next.variants.find((v) => v.key === nextVariantKey) ?? next.variants[0]
    const values = form.getValues()
    setPresetKey(next.key)
    setVariantKey(nextVariant.key)
    setPickingPreset(false)
    setFormError(null)
    form.setValue("protocol", nextVariant.protocol, { shouldDirty: true })
    form.setValue("base_url", nextVariant.baseUrl, { shouldDirty: true, shouldValidate: Boolean(nextVariant.baseUrl) })
    const suggestedId = uniqueId(next.id, takenProviderIds)
    if (!values.name.trim() || values.name === applied.name) form.setValue("name", next.name, { shouldDirty: true })
    if (!values.id.trim() || values.id === applied.id) {
      form.setValue("id", suggestedId, { shouldDirty: true, shouldValidate: true })
    }
    setApplied({ name: next.name, id: suggestedId })
    if (next.headers && values.headers.every((row) => !row.key.trim())) {
      form.setValue(
        "headers",
        Object.entries(next.headers).map(([key, value]) => ({ key, value })),
        { shouldDirty: true },
      )
    }
    if (next.key !== presetKey) {
      setSuggested(new Set((next.models ?? []).filter((m) => m.checked).map((m) => m.id)))
    }
  }

  const mutation = useMutation({
    mutationFn: async (values: ProviderFormValues): Promise<ProviderSaved> => {
      if (provider) {
        const patch: ProviderPatch = {
          name: values.name.trim() || provider.id,
          protocol: values.protocol,
          base_url: values.base_url.trim(),
          headers: toRecord(values.headers),
          enabled: values.enabled,
          note: values.note.trim(),
        }
        if (values.clear_api_key) patch.clear_api_key = true
        else if (values.api_key.trim()) patch.api_key = values.api_key.trim()
        const saved = await adminApi.updateProvider(provider.id, patch)
        return { provider: saved, created: false, models: [], modelErrors: [] }
      }
      const saved = await adminApi.createProvider({
        id: values.id.trim(),
        name: values.name.trim() || values.id.trim(),
        protocol: values.protocol,
        base_url: values.base_url.trim(),
        api_key: values.api_key.trim() || undefined,
        headers: toRecord(values.headers),
        enabled: values.enabled,
        note: values.note.trim(),
      })
      // 建议模型逐个创建:一个失败不连累其他,失败的原因一并带回去
      const createdModels: string[] = []
      const modelErrors: string[] = []
      for (const m of suggestionList) {
        if (!suggested.has(m.id)) continue
        try {
          await adminApi.createModel({
            id: m.finalId,
            provider_id: saved.id,
            kind: m.kind,
            model: m.model,
            params: m.params ?? {},
          })
          createdModels.push(m.finalId)
        } catch (error) {
          modelErrors.push(`${m.finalId}:${errorMessage(error)}`)
        }
      }
      return { provider: saved, created: true, models: createdModels, modelErrors }
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: qk.providers })
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
      // 地址或密钥可能变了,旧的测试结论不再作数
      void queryClient.resetQueries({ queryKey: probeKey(result.provider.id), exact: true })
      onSaved(result)
      onOpenChange(false)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        form.setError("id", { message: error.message })
        return
      }
      setFormError(errorMessage(error))
    },
  })

  const commit = form.handleSubmit((values) => {
    setFormError(null)
    if (!provider && takenProviderIds.includes(values.id.trim())) {
      form.setError("id", { message: `服务商 ${values.id.trim()} 已存在` })
      return
    }
    if (incompatible.length > 0) return
    mutation.mutate(values)
  })

  const keyOptional = preset?.keyOptional || protocol === "system_one"

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className={SHEET_PANEL}>
        <SheetHeader className="shrink-0 gap-1 px-4 pt-4 pb-3">
          <SheetTitle>{editing ? `编辑服务商 · ${provider?.name}` : "新建服务商"}</SheetTitle>
          <SheetDescription>
            {editing
              ? "保存后立即生效:引用这家服务商的模型下一次调用就走新的地址与密钥。"
              : "从预设开始,协议、接口地址与建议模型会一次带齐;只需要再填密钥。"}
          </SheetDescription>
        </SheetHeader>
        <Separator />
        <form id="provider-form" noValidate onSubmit={commit} className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-4 py-4">
          {formError ? (
            <Notice tone="destructive" title="保存失败">
              {formError}
            </Notice>
          ) : null}

          {!editing ? (
            <FieldSet className="gap-3">
              <div className="flex items-center justify-between gap-3">
                <FieldLegend variant="label" className="mb-0">
                  预设
                </FieldLegend>
                {!pickingPreset && preset ? (
                  <Button type="button" variant="ghost" size="xs" onClick={() => setPickingPreset(true)}>
                    <RotateCcwIcon /> 更换预设
                  </Button>
                ) : null}
              </div>
              {pickingPreset || !preset ? (
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label="服务商预设">
                  {PROVIDER_PRESETS.map((item) => {
                    const active = item.key === presetKey
                    const protocols = [...new Set(item.variants.map((v) => v.protocol))]
                    return (
                      <button
                        key={item.key}
                        type="button"
                        role="radio"
                        aria-checked={active}
                        onClick={() => applyPreset(item)}
                        className={cn(
                          "flex min-w-0 items-center gap-3 rounded-xl border px-3 py-2.5 text-start transition-colors outline-none hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50",
                          active && "border-primary bg-primary/5",
                        )}
                      >
                        <ProviderLogo name={item.name} size="sm" />
                        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                          <span className="truncate text-sm font-medium">{item.name}</span>
                          <span className="truncate text-xs text-muted-foreground">
                            {protocols.map((p) => labelOf.protocol(p).label).join(" / ")} · {item.description}
                          </span>
                        </span>
                      </button>
                    )
                  })}
                </div>
              ) : (
                <div className="flex flex-col gap-3 rounded-xl border px-3 py-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <ProviderLogo name={preset.name} size="sm" />
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="truncate text-sm font-medium">{preset.name}</span>
                      <span className="truncate text-xs text-muted-foreground">{preset.description}</span>
                    </div>
                  </div>
                  {preset.variants.length > 1 ? (
                    <div className="flex flex-col gap-1.5">
                      <ToggleGroup
                        type="single"
                        variant="outline"
                        size="sm"
                        spacing={0}
                        value={variant?.key ?? ""}
                        onValueChange={(next) => {
                          if (next) applyPreset(preset, next)
                        }}
                        aria-label="接口变体"
                      >
                        {preset.variants.map((v) => (
                          <ToggleGroupItem key={v.key} value={v.key}>
                            {v.label}
                          </ToggleGroupItem>
                        ))}
                      </ToggleGroup>
                      {variant?.hint ? <p className="text-xs text-muted-foreground">{variant.hint}</p> : null}
                    </div>
                  ) : null}
                </div>
              )}
            </FieldSet>
          ) : null}

          <FieldGroup className="gap-5">
            <div className="grid gap-5 sm:grid-cols-2">
              <Field data-invalid={Boolean(errors.name) || undefined}>
                <FieldLabel htmlFor="provider-form-name">名称</FieldLabel>
                <Input id="provider-form-name" placeholder="例如 SiliconFlow" aria-invalid={Boolean(errors.name) || undefined} {...form.register("name")} />
                <FieldError errors={[errors.name]} />
              </Field>
              <Field data-invalid={Boolean(errors.id) || undefined}>
                <FieldLabel htmlFor="provider-form-id">服务商 ID</FieldLabel>
                <Input
                  id="provider-form-id"
                  placeholder="例如 siliconflow"
                  readOnly={editing}
                  className={cn("font-mono", editing && "text-muted-foreground")}
                  aria-invalid={Boolean(errors.id) || undefined}
                  {...form.register("id")}
                />
                {errors.id ? (
                  <FieldError errors={[errors.id]} />
                ) : (
                  <FieldDescription className="text-xs">{editing ? "ID 创建后不可修改" : "模型按它挂靠,创建后不可修改"}</FieldDescription>
                )}
              </Field>
            </div>

            <Controller
              control={form.control}
              name="protocol"
              render={({ field }) => (
                <Field data-invalid={incompatible.length > 0 || undefined}>
                  <FieldLabel htmlFor="provider-form-protocol">协议</FieldLabel>
                  <ToggleGroup
                    id="provider-form-protocol"
                    type="single"
                    variant="outline"
                    size="sm"
                    spacing={0}
                    value={field.value}
                    onValueChange={(next) => {
                      if (next) field.onChange(next as Protocol)
                    }}
                    aria-label="协议"
                    className="flex-wrap"
                  >
                    {PROTOCOLS.map((p) => (
                      <ToggleGroupItem key={p} value={p}>
                        {labelOf.protocol(p).label}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                  {incompatible.length > 0 ? (
                    <FieldError>
                      这些模型不支持 {labelOf.protocol(field.value).label} 协议:{incompatible.map((m) => m.id).join("、")}。先删除或改挂到别的服务商。
                    </FieldError>
                  ) : (
                    <FieldDescription className="text-xs">
                      {labelOf.protocol(field.value).hint} · 可建{" "}
                      {kindsFor(field.value)
                        .map((k) => labelOf.kind(k).label)
                        .join(" / ")}
                      模型
                    </FieldDescription>
                  )}
                </Field>
              )}
            />

            <Field data-invalid={Boolean(errors.base_url) || undefined}>
              <FieldLabel htmlFor="provider-form-url">接口地址</FieldLabel>
              <Input
                id="provider-form-url"
                inputMode="url"
                placeholder={protocol === "anthropic" ? "https://api.example.com" : "https://api.example.com/v1"}
                className="font-mono"
                aria-invalid={Boolean(errors.base_url) || undefined}
                {...form.register("base_url")}
              />
              {errors.base_url ? (
                <FieldError errors={[errors.base_url]} />
              ) : (
                <FieldDescription className="text-xs">
                  {protocol === "openai"
                    ? "到 /v1 为止;/chat/completions、/embeddings、/rerank 由系统拼接。"
                    : protocol === "anthropic"
                      ? "不带 /v1/messages;带不带 /v1 都可以。"
                      : "System One 接口的根地址,系统会请求 {地址}/systemone。"}
                </FieldDescription>
              )}
            </Field>

            <Field>
              <FieldLabel htmlFor="provider-form-key">API 密钥</FieldLabel>
              <InputGroup>
                <InputGroupInput
                  id="provider-form-key"
                  type={revealKey ? "text" : "password"}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={clearKey}
                  className="font-mono"
                  placeholder={
                    editing ? (provider?.api_key_set ? "留空则保持现有密钥" : "尚未设置密钥") : keyOptional ? "可选" : "sk-…"
                  }
                  {...form.register("api_key")}
                />
                <InputGroupAddon align="inline-end">
                  <InputGroupButton size="icon-xs" aria-label={revealKey ? "隐藏密钥" : "显示密钥"} onClick={() => setRevealKey((v) => !v)}>
                    {revealKey ? <EyeOffIcon /> : <EyeIcon />}
                  </InputGroupButton>
                </InputGroupAddon>
              </InputGroup>
              <FieldDescription className="text-xs">
                {editing
                  ? provider?.api_key_set
                    ? `当前密钥 ${provider.api_key_hint};密钥只写不读,保存后同样只显示末四位。`
                    : "当前没有密钥。"
                  : keyOptional
                    ? "本机或自建服务通常不需要密钥,可以留空。"
                    : "只写不读:保存后页面上只显示末四位。"}
              </FieldDescription>
              {editing && provider?.api_key_set ? (
                <Controller
                  control={form.control}
                  name="clear_api_key"
                  render={({ field }) => (
                    <Field orientation="horizontal" className="mt-1">
                      <Checkbox id="provider-form-clear-key" checked={field.value} onCheckedChange={(checked) => field.onChange(checked === true)} />
                      <FieldLabel htmlFor="provider-form-clear-key" className="font-normal">
                        清除已保存的密钥
                      </FieldLabel>
                    </Field>
                  )}
                />
              ) : null}
            </Field>

            <FieldSet className="gap-2">
              <div className="flex items-center justify-between gap-2">
                <FieldLegend variant="label" className="mb-0">
                  额外请求头
                </FieldLegend>
                <Button type="button" variant="ghost" size="xs" onClick={() => headers.append({ key: "", value: "" })}>
                  <PlusIcon /> 添加
                </Button>
              </div>
              {headers.fields.length === 0 ? (
                <p className="text-xs text-muted-foreground">没有额外请求头。OpenRouter 之类的聚合服务可以在这里带上应用名。</p>
              ) : (
                <div className="flex flex-col gap-2">
                  {headers.fields.map((row, index) => {
                    const rowError = errors.headers?.[index]?.key
                    return (
                      <div key={row.id} className="flex flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <Input
                            aria-label={`请求头 ${index + 1} 名称`}
                            placeholder="Header-Name"
                            className="h-8 flex-1 font-mono text-xs"
                            aria-invalid={Boolean(rowError) || undefined}
                            {...form.register(`headers.${index}.key`)}
                          />
                          <Input aria-label={`请求头 ${index + 1} 取值`} placeholder="取值" className="h-8 flex-[1.4] font-mono text-xs" {...form.register(`headers.${index}.value`)} />
                          <Button type="button" variant="ghost" size="icon-sm" aria-label={`删除请求头 ${index + 1}`} onClick={() => headers.remove(index)}>
                            <Trash2Icon />
                          </Button>
                        </div>
                        {rowError ? <FieldError className="text-xs">{rowError.message}</FieldError> : null}
                      </div>
                    )
                  })}
                </div>
              )}
            </FieldSet>

            {!editing && suggestionList.length > 0 ? (
              <FieldSet className="gap-2">
                <FieldLegend variant="label" className="mb-0">
                  同时注册模型
                </FieldLegend>
                <FieldDescription className="text-xs">
                  服务商建好后按顺序注册;注册名与已有模型重名时自动加后缀。之后可以在「模型绑定」里把它们分派给各环节。
                </FieldDescription>
                <div className="flex flex-col gap-2">
                  {suggestionList.map((m) => (
                    <SuggestedModelOption
                      key={m.id}
                      model={m}
                      finalId={m.finalId}
                      checked={suggested.has(m.id)}
                      onCheckedChange={(checked) =>
                        setSuggested((current) => {
                          const next = new Set(current)
                          if (checked) next.add(m.id)
                          else next.delete(m.id)
                          return next
                        })
                      }
                    />
                  ))}
                </div>
              </FieldSet>
            ) : null}

            <Controller
              control={form.control}
              name="enabled"
              render={({ field }) => (
                <Field orientation="horizontal" className="rounded-xl border px-3 py-2.5">
                  <FieldContent>
                    <FieldLabel htmlFor="provider-form-enabled">启用</FieldLabel>
                    <FieldDescription className="text-xs">停用后,挂在这家服务商下的模型全部不可用,绑定它们的环节回退到环境变量配置。</FieldDescription>
                  </FieldContent>
                  <Switch id="provider-form-enabled" checked={field.value} onCheckedChange={field.onChange} />
                </Field>
              )}
            />

            <Field data-invalid={Boolean(errors.note) || undefined}>
              <FieldLabel htmlFor="provider-form-note">备注</FieldLabel>
              <Textarea id="provider-form-note" rows={2} placeholder="例如:谁的账号、额度与用途" aria-invalid={Boolean(errors.note) || undefined} {...form.register("note")} />
              <FieldError errors={[errors.note]} />
            </Field>
          </FieldGroup>
        </form>
        <Separator />
        <SheetFooter className="shrink-0 flex-row items-center justify-end gap-2 px-4 py-3">
          <SheetClose asChild>
            <Button type="button" size="sm" variant="outline">
              取消
            </Button>
          </SheetClose>
          <Button type="submit" form="provider-form" size="sm" disabled={mutation.isPending}>
            {mutation.isPending ? <Spinner /> : null}
            {editing ? "保存修改" : suggested.size > 0 && suggestionList.length > 0 ? "创建服务商与模型" : "创建服务商"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

function SuggestedModelOption({
  model,
  finalId,
  checked,
  onCheckedChange,
}: {
  model: PresetModel
  finalId: string
  checked: boolean
  onCheckedChange: (checked: boolean) => void
}) {
  const id = `provider-form-suggest-${model.id}`
  const summary = paramsSummary(model.kind, model.params ?? {})
  return (
    <FieldLabel htmlFor={id} className="w-full">
      <Field orientation="horizontal" className="px-3! py-2.5!">
        <Checkbox id={id} checked={checked} onCheckedChange={(value) => onCheckedChange(value === true)} />
        <FieldContent className="min-w-0">
          <FieldTitle className="min-w-0">
            <span className="truncate font-mono">{finalId}</span>
            <KindBadge kind={model.kind} />
            {finalId !== model.id ? <Badge variant="outline">已有同名,改用新注册名</Badge> : null}
          </FieldTitle>
          <FieldDescription className="truncate font-mono text-xs">
            {model.model}
            {summary.length > 0 ? ` · ${summary.join(" · ")}` : ""}
          </FieldDescription>
        </FieldContent>
      </Field>
    </FieldLabel>
  )
}
