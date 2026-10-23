"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ListIcon, RefreshCwIcon, WandSparklesIcon } from "lucide-react"
import { useMemo, useState } from "react"
import { Controller, useForm, useWatch } from "react-hook-form"
import { z } from "zod"

import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput, InputGroupText } from "@/components/ui/input-group"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { ModelEntry, ModelInput, ModelKind, ModelParams, Provider } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { ProtocolBadge } from "./bits"
import { useProviderProbe } from "./lib/queries"
import { ID_PATTERN, ID_RULE, JUDGE_ALIAS, kindsFor, slugify, uniqueId, usageLabel } from "./lib/registry"
import { SHEET_PANEL } from "./sheet-styles"

/**
 * 注册一个模型,或修改它。模型挂在服务商下,类型由服务商的协议决定能选哪些;参数按类型出现。
 * 模型名可以直接敲,也可以从服务商「测试连接」列出的清单里挑。注册名是别处引用它的名字,创建后不可改。
 */
function numeric(label: string, rule: { min?: number; max?: number; int?: boolean; above?: number }) {
  return z
    .string()
    .trim()
    .superRefine((raw, ctx) => {
      if (raw === "") return
      const n = Number(raw)
      if (!Number.isFinite(n)) {
        ctx.addIssue({ code: "custom", message: `${label}需要是数字` })
        return
      }
      if (rule.int && !Number.isInteger(n)) {
        ctx.addIssue({ code: "custom", message: `${label}需要是整数` })
        return
      }
      if (rule.above !== undefined && n <= rule.above) {
        ctx.addIssue({ code: "custom", message: `${label}需要大于 ${rule.above}` })
        return
      }
      if (rule.min !== undefined && n < rule.min) {
        ctx.addIssue({ code: "custom", message: `${label}不能小于 ${rule.min}` })
        return
      }
      if (rule.max !== undefined && n > rule.max) ctx.addIssue({ code: "custom", message: `${label}不能大于 ${rule.max}` })
    })
}

const modelSchema = z
  .object({
    provider_id: z.string().min(1, "请选择服务商"),
    kind: z.enum(["chat", "embedding", "rerank", "judge"]),
    id: z
      .string()
      .trim()
      .min(1, "请填写注册名")
      .regex(ID_PATTERN, `注册名${ID_RULE}`)
      .refine((value) => value !== JUDGE_ALIAS, "「jev」是保留名,始终指当前绑定的适用性判官,请换一个"),
    model: z.string().trim().min(1, "请填写服务商侧的模型名"),
    temperatureMode: z.enum(["default", "set", "omit"]),
    temperature: numeric("温度", { min: 0, max: 2 }),
    max_tokens: numeric("输出上限", { min: 1, int: true }),
    timeout: numeric("超时", { above: 0 }),
    concurrency: numeric("并发", { min: 1, int: true }),
    retries: numeric("重试次数", { min: 0, max: 10, int: true }),
    api_version: z.string().trim().max(40),
    extra: z.string().superRefine((raw, ctx) => {
      if (!raw.trim()) return
      try {
        const value: unknown = JSON.parse(raw)
        if (!value || typeof value !== "object" || Array.isArray(value)) {
          ctx.addIssue({ code: "custom", message: 'extra 需要是 JSON 对象,例如 {"thinking": {"type": "disabled"}}' })
        }
      } catch (error) {
        ctx.addIssue({ code: "custom", message: `JSON 格式错误:${(error as Error).message}` })
      }
    }),
    dimensions: numeric("向量维度", { min: 1, int: true }),
    top_n: numeric("top_n", { min: 1, int: true }),
    enabled: z.boolean(),
    note: z.string().trim().max(500, "备注不超过 500 个字符"),
  })
  .superRefine((values, ctx) => {
    if (values.kind === "chat" && values.temperatureMode === "set" && values.temperature.trim() === "") {
      ctx.addIssue({ code: "custom", path: ["temperature"], message: "请填写温度,或改为「默认」" })
    }
  })
type ModelFormValues = z.infer<typeof modelSchema>

const text = (value: number | string | null | undefined) => (value === null || value === undefined ? "" : String(value))

function blankValues(providerId: string, kind: ModelKind, model = ""): ModelFormValues {
  return {
    provider_id: providerId,
    kind,
    id: "",
    model,
    temperatureMode: "default",
    temperature: "",
    max_tokens: "",
    timeout: "",
    concurrency: "",
    retries: "",
    api_version: "",
    extra: "",
    dimensions: "",
    top_n: "",
    enabled: true,
    note: "",
  }
}

function toValues(model: ModelEntry): ModelFormValues {
  const p = model.params ?? {}
  return {
    provider_id: model.provider_id,
    kind: model.kind,
    id: model.id,
    model: model.model,
    temperatureMode: !("temperature" in p) ? "default" : p.temperature === null ? "omit" : "set",
    temperature: text(p.temperature),
    max_tokens: text(p.max_tokens),
    timeout: text(p.timeout),
    concurrency: text(p.concurrency),
    retries: text(p.retries),
    api_version: p.api_version ?? "",
    extra: p.extra && Object.keys(p.extra).length > 0 ? JSON.stringify(p.extra, null, 2) : "",
    dimensions: text(p.dimensions),
    top_n: text(p.top_n),
    enabled: model.enabled,
    note: model.note,
  }
}

function toParams(values: ModelFormValues, protocol: string): ModelParams {
  const num = (raw: string) => (raw.trim() === "" ? undefined : Number(raw))
  const params: ModelParams = {}
  const put = <K extends keyof ModelParams>(key: K, value: ModelParams[K] | undefined) => {
    if (value !== undefined) params[key] = value
  }
  if (values.kind === "chat") {
    if (values.temperatureMode === "set") put("temperature", num(values.temperature))
    if (values.temperatureMode === "omit") params.temperature = null
    put("max_tokens", num(values.max_tokens))
    put("timeout", num(values.timeout))
    put("concurrency", num(values.concurrency))
    put("retries", num(values.retries))
    if (protocol === "anthropic" && values.api_version.trim()) put("api_version", values.api_version.trim())
    if (values.extra.trim()) put("extra", JSON.parse(values.extra) as Record<string, unknown>)
  }
  if (values.kind === "embedding") put("dimensions", num(values.dimensions))
  if (values.kind === "rerank") put("top_n", num(values.top_n))
  if (values.kind === "judge") {
    put("timeout", num(values.timeout))
    put("concurrency", num(values.concurrency))
    put("retries", num(values.retries))
  }
  return params
}

export interface ModelFormInitial {
  providerId?: string
  model?: string
  kind?: ModelKind
}

export function ModelFormSheet({
  open,
  onOpenChange,
  model,
  initial,
  providers,
  models,
  onSaved,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 有值即编辑 */
  model: ModelEntry | null
  /** 新建时的预填:从某个服务商的「添加模型」或测试结果里点进来 */
  initial?: ModelFormInitial | null
  providers: Provider[]
  models: ModelEntry[]
  onSaved: (model: ModelEntry, created: boolean) => void
}) {
  const queryClient = useQueryClient()
  const editing = Boolean(model)

  const initialValues = (): ModelFormValues => {
    if (model) return toValues(model)
    const provider = providers.find((p) => p.id === initial?.providerId) ?? providers.find((p) => p.enabled) ?? providers[0]
    const kinds = kindsFor(provider?.protocol)
    const kind = initial?.kind && kinds.includes(initial.kind) ? initial.kind : (kinds[0] ?? "chat")
    const values = blankValues(provider?.id ?? "", kind, initial?.model ?? "")
    if (initial?.model) values.id = uniqueId(slugify(initial.model.split("/").pop() ?? ""), models.map((m) => m.id))
    return values
  }

  const form = useForm<ModelFormValues>({
    resolver: zodResolver(modelSchema),
    defaultValues: initialValues(),
    mode: "onTouched",
  })
  const errors = form.formState.errors
  const [idTouched, setIdTouched] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [seen, setSeen] = useState({ open, model, initial })

  if (seen.open !== open || seen.model !== model || seen.initial !== initial) {
    setSeen({ open, model, initial })
    if (open) {
      form.reset(initialValues())
      setIdTouched(false)
      setFormError(null)
      setPickerOpen(false)
    }
  }

  const providerId = useWatch({ control: form.control, name: "provider_id" })
  const kind = useWatch({ control: form.control, name: "kind" })
  const temperatureMode = useWatch({ control: form.control, name: "temperatureMode" })
  const provider = providers.find((p) => p.id === providerId)
  const protocol = provider?.protocol ?? ""
  const allowedKinds = kindsFor(provider?.protocol)
  const probe = useProviderProbe(protocol === "system_one" ? null : providerId)
  const listed = probe.data?.models ?? []
  const takenIds = useMemo(() => models.map((m) => m.id), [models])
  const usedBy = model?.used_by ?? []
  const kindLocked = editing && usedBy.length > 0

  function suggestId(modelName: string) {
    if (editing || idTouched) return
    const base = slugify(modelName.split("/").pop() ?? "", "")
    form.setValue("id", base ? uniqueId(base, takenIds) : "", { shouldDirty: true })
  }

  const mutation = useMutation({
    mutationFn: async (values: ModelFormValues) => {
      const params = toParams(values, protocol)
      if (model) {
        return adminApi.updateModel(model.id, {
          provider_id: values.provider_id,
          kind: values.kind,
          model: values.model.trim(),
          params,
          enabled: values.enabled,
          note: values.note.trim(),
        })
      }
      const input: ModelInput = {
        id: values.id.trim(),
        provider_id: values.provider_id,
        kind: values.kind,
        model: values.model.trim(),
        params,
        enabled: values.enabled,
        note: values.note.trim(),
      }
      return adminApi.createModel(input)
    },
    onSuccess: (saved) => {
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.providers })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
      void queryClient.invalidateQueries({ queryKey: qk.settings })
      onSaved(saved, !editing)
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
    if (!editing && takenIds.includes(values.id.trim())) {
      form.setError("id", { message: `注册名 ${values.id.trim()} 已被占用` })
      return
    }
    if (!allowedKinds.includes(values.kind)) {
      form.setError("kind", { message: `${labelOf.protocol(protocol).label} 服务商不能建${labelOf.kind(values.kind).label}模型` })
      return
    }
    mutation.mutate(values)
  })

  const boundAsEmbedding = usedBy.includes("model.embedding")

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className={SHEET_PANEL}>
        <SheetHeader className="shrink-0 gap-1 px-4 pt-4 pb-3">
          <SheetTitle>{editing ? `编辑模型 · ${model?.id}` : "注册模型"}</SheetTitle>
          <SheetDescription>
            {editing
              ? "保存后立即生效;正在使用它的环节下一次调用就按新参数。"
              : "模型挂在服务商下;注册后到「模型绑定」把它分派给某个环节,或加进网关路由。"}
          </SheetDescription>
        </SheetHeader>
        <Separator />
        <form id="model-form" noValidate onSubmit={commit} className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-4 py-4">
          {formError ? (
            <Notice tone="destructive" title="保存失败">
              {formError}
            </Notice>
          ) : null}
          <FieldGroup className="gap-5">
            <Controller
              control={form.control}
              name="provider_id"
              render={({ field }) => (
                <Field data-invalid={Boolean(errors.provider_id) || undefined}>
                  <FieldLabel htmlFor="model-form-provider">服务商</FieldLabel>
                  <Select
                    value={field.value}
                    onValueChange={(value) => {
                      if (!value) return
                      field.onChange(value)
                      const next = providers.find((p) => p.id === value)
                      const kinds = kindsFor(next?.protocol)
                      if (!kinds.includes(form.getValues("kind")) && kinds[0]) {
                        form.setValue("kind", kinds[0], { shouldDirty: true })
                      }
                    }}
                  >
                    <SelectTrigger id="model-form-provider" className="w-full" onBlur={field.onBlur}>
                      <SelectValue placeholder="选择服务商" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {providers.map((p) => (
                          <SelectItem key={p.id} value={p.id}>
                            <span className="flex min-w-0 flex-1 items-center gap-2">
                              <span className="truncate">{p.name}</span>
                              <ProtocolBadge protocol={p.protocol} />
                              {!p.enabled ? <Badge variant="outline">已停用</Badge> : null}
                            </span>
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <FieldError errors={[errors.provider_id]} />
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="kind"
              render={({ field }) => (
                <Field data-invalid={Boolean(errors.kind) || undefined}>
                  <FieldLabel htmlFor="model-form-kind">类型</FieldLabel>
                  <ToggleGroup
                    id="model-form-kind"
                    type="single"
                    variant="outline"
                    size="sm"
                    spacing={0}
                    disabled={kindLocked}
                    value={field.value}
                    onValueChange={(next) => {
                      if (next) field.onChange(next as ModelKind)
                    }}
                    aria-label="模型类型"
                  >
                    {(["chat", "embedding", "rerank", "judge"] as ModelKind[]).map((k) => (
                      <ToggleGroupItem key={k} value={k} disabled={!allowedKinds.includes(k)}>
                        {labelOf.kind(k).label}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                  {errors.kind ? (
                    <FieldError errors={[errors.kind]} />
                  ) : (
                    <FieldDescription className="text-xs">
                      {kindLocked
                        ? `正被 ${usedBy.map(usageLabel).join("、")} 使用,不能更改类型。`
                        : provider
                          ? `${labelOf.protocol(protocol).label} 服务商可建:${allowedKinds.map((k) => labelOf.kind(k).label).join(" / ")}`
                          : "先选服务商"}
                    </FieldDescription>
                  )}
                </Field>
              )}
            />

            <Field data-invalid={Boolean(errors.model) || undefined}>
              <FieldLabel htmlFor="model-form-model">模型名</FieldLabel>
              <InputGroup>
                <InputGroupInput
                  id="model-form-model"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={kind === "embedding" ? "BAAI/bge-m3" : kind === "judge" ? "jev-1.13.0" : "服务商侧的模型名"}
                  className="font-mono"
                  aria-invalid={Boolean(errors.model) || undefined}
                  {...form.register("model", { onChange: (event) => suggestId(event.target.value) })}
                />
                {protocol && protocol !== "system_one" ? (
                  <InputGroupAddon align="inline-end">
                    <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
                      <PopoverTrigger asChild>
                        <InputGroupButton size="xs" aria-label="从服务商的模型列表选择">
                          <ListIcon />
                          {listed.length > 0 ? listed.length : "列表"}
                        </InputGroupButton>
                      </PopoverTrigger>
                      <PopoverContent align="end" className="w-80 gap-0 p-0">
                        {listed.length > 0 ? (
                          <Command>
                            <CommandInput placeholder="筛选模型名" />
                            <CommandList>
                              <CommandEmpty>没有匹配的模型名</CommandEmpty>
                              <CommandGroup heading={`${provider?.name} 列出的模型`}>
                                {listed.map((name) => (
                                  <CommandItem
                                    key={name}
                                    value={name}
                                    onSelect={() => {
                                      form.setValue("model", name, { shouldDirty: true, shouldValidate: true })
                                      suggestId(name)
                                      setPickerOpen(false)
                                    }}
                                    className="font-mono text-xs"
                                  >
                                    <span className="truncate">{name}</span>
                                  </CommandItem>
                                ))}
                              </CommandGroup>
                            </CommandList>
                          </Command>
                        ) : (
                          <div className="flex flex-col items-start gap-3 p-4">
                            <p className="text-sm text-muted-foreground">
                              {probe.data && !probe.data.ok
                                ? `上次测试没有连通:${probe.data.detail}`
                                : "还没有这家服务商的模型列表。测试一次连接,服务商会列出可用的模型。"}
                            </p>
                            <Button type="button" size="sm" variant="outline" disabled={probe.isFetching || !providerId} onClick={() => void probe.refetch()}>
                              {probe.isFetching ? <Spinner /> : <RefreshCwIcon />}
                              测试连接并获取列表
                            </Button>
                          </div>
                        )}
                      </PopoverContent>
                    </Popover>
                  </InputGroupAddon>
                ) : null}
              </InputGroup>
              {errors.model ? (
                <FieldError errors={[errors.model]} />
              ) : (
                <FieldDescription className="text-xs">请求里发给服务商的 model 字段,区分大小写。</FieldDescription>
              )}
            </Field>

            <Field data-invalid={Boolean(errors.id) || undefined}>
              <FieldLabel htmlFor="model-form-id">注册名</FieldLabel>
              <Input
                id="model-form-id"
                readOnly={editing}
                autoComplete="off"
                spellCheck={false}
                placeholder="例如 bge-m3"
                className={cn("font-mono", editing && "text-muted-foreground")}
                aria-invalid={Boolean(errors.id) || undefined}
                {...form.register("id", { onChange: () => setIdTouched(true) })}
              />
              {errors.id ? (
                <FieldError errors={[errors.id]} />
              ) : (
                <FieldDescription className="text-xs">
                  {editing
                    ? "注册名创建后不可修改。"
                    : idTouched
                      ? "角色绑定与网关路由按这个名字引用模型,创建后不可修改。"
                      : "按模型名自动生成,也可以自己改;创建后不可修改。"}
                </FieldDescription>
              )}
            </Field>
          </FieldGroup>

          <FieldSet className="gap-4">
            <div className="flex flex-col gap-1">
              <FieldLegend variant="label" className="mb-0">
                参数
              </FieldLegend>
              <FieldDescription className="text-xs">留空的项用系统默认值。</FieldDescription>
            </div>

            {kind === "chat" ? (
              <>
                <Controller
                  control={form.control}
                  name="temperatureMode"
                  render={({ field }) => (
                    <Field data-invalid={Boolean(errors.temperature) || undefined}>
                      <FieldLabel htmlFor="model-form-temperature">温度</FieldLabel>
                      <div className="flex flex-wrap items-center gap-2">
                        <ToggleGroup
                          type="single"
                          variant="outline"
                          size="sm"
                          spacing={0}
                          value={field.value}
                          onValueChange={(next) => {
                            if (next) field.onChange(next as ModelFormValues["temperatureMode"])
                          }}
                          aria-label="温度设置方式"
                        >
                          <ToggleGroupItem value="default">默认</ToggleGroupItem>
                          <ToggleGroupItem value="set">指定</ToggleGroupItem>
                          <ToggleGroupItem value="omit">不传</ToggleGroupItem>
                        </ToggleGroup>
                        {temperatureMode === "set" ? (
                          <Input
                            id="model-form-temperature"
                            inputMode="decimal"
                            placeholder="0.2"
                            className="h-8 w-24 tabular-nums"
                            aria-invalid={Boolean(errors.temperature) || undefined}
                            {...form.register("temperature")}
                          />
                        ) : null}
                      </div>
                      {errors.temperature ? (
                        <FieldError errors={[errors.temperature]} />
                      ) : (
                        <FieldDescription className="text-xs">
                          {temperatureMode === "omit"
                            ? "请求里不带 temperature:部分推理模型只接受默认温度。"
                            : temperatureMode === "set"
                              ? "0 ~ 2,越低越稳定。"
                              : "使用系统默认温度。"}
                        </FieldDescription>
                      )}
                    </Field>
                  )}
                />
                <div className="grid gap-4 sm:grid-cols-2">
                  <NumberInput form={form} name="max_tokens" label="输出上限" unit="tokens" placeholder="默认" />
                  <NumberInput form={form} name="timeout" label="超时" unit="秒" placeholder="默认" decimal />
                  <NumberInput form={form} name="concurrency" label="并发" placeholder="默认" />
                  <NumberInput form={form} name="retries" label="重试次数" placeholder="默认" />
                </div>
                {protocol === "anthropic" ? (
                  <Field data-invalid={Boolean(errors.api_version) || undefined}>
                    <FieldLabel htmlFor="model-form-api-version">API 版本</FieldLabel>
                    <Input id="model-form-api-version" placeholder="2023-06-01" className="font-mono" {...form.register("api_version")} />
                    <FieldDescription className="text-xs">anthropic-version 请求头;留空用默认值。</FieldDescription>
                  </Field>
                ) : null}
                <Field data-invalid={Boolean(errors.extra) || undefined}>
                  <div className="flex items-center justify-between gap-2">
                    <FieldLabel htmlFor="model-form-extra">extra</FieldLabel>
                    <Button
                      type="button"
                      variant="ghost"
                      size="xs"
                      onClick={() => {
                        const raw = form.getValues("extra")
                        try {
                          if (raw.trim()) {
                            form.setValue("extra", JSON.stringify(JSON.parse(raw), null, 2), { shouldValidate: true })
                          }
                        } catch {
                          void form.trigger("extra")
                        }
                      }}
                    >
                      <WandSparklesIcon /> 格式化
                    </Button>
                  </div>
                  <Textarea
                    id="model-form-extra"
                    rows={4}
                    spellCheck={false}
                    placeholder={'{"thinking": {"type": "disabled"}}'}
                    className="font-mono text-xs"
                    aria-invalid={Boolean(errors.extra) || undefined}
                    {...form.register("extra")}
                  />
                  {errors.extra ? (
                    <FieldError errors={[errors.extra]} />
                  ) : (
                    <FieldDescription className="text-xs">JSON 对象,原样合并进请求体——例如关掉思考模式,或传服务商特有的参数。</FieldDescription>
                  )}
                </Field>
              </>
            ) : null}

            {kind === "embedding" ? (
              <NumberInput
                form={form}
                name="dimensions"
                label="向量维度"
                unit="维"
                placeholder="1024"
                description={
                  boundAsEmbedding
                    ? "正作为向量模型使用:维度变化需要重启服务,并在「系统状态」页重建向量索引。"
                    : "与模型实际输出一致;测试时会核对。"
                }
              />
            ) : null}
            {kind === "rerank" ? (
              <NumberInput form={form} name="top_n" label="top_n" placeholder="默认" description="精排接口一次返回的条数;留空时按请求条数 × 精排保留倍数。" />
            ) : null}
            {kind === "judge" ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <NumberInput form={form} name="timeout" label="超时" unit="秒" placeholder="默认" decimal />
                <NumberInput form={form} name="concurrency" label="并发" placeholder="默认" />
                <NumberInput form={form} name="retries" label="重试次数" placeholder="默认" />
              </div>
            ) : null}
          </FieldSet>

          <FieldGroup className="gap-5">
            <Controller
              control={form.control}
              name="enabled"
              render={({ field }) => (
                <Field orientation="horizontal" className="rounded-xl border px-3 py-2.5">
                  <FieldContent>
                    <FieldLabel htmlFor="model-form-enabled">启用</FieldLabel>
                    <FieldDescription className="text-xs">停用后绑定它的环节回退到环境变量配置,网关路由会跳过它。</FieldDescription>
                  </FieldContent>
                  <Switch id="model-form-enabled" checked={field.value} onCheckedChange={field.onChange} />
                </Field>
              )}
            />
            <Field data-invalid={Boolean(errors.note) || undefined}>
              <FieldLabel htmlFor="model-form-note">备注</FieldLabel>
              <Textarea id="model-form-note" rows={2} placeholder="用途、限额或注意事项" {...form.register("note")} />
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
          <Button type="submit" form="model-form" size="sm" disabled={mutation.isPending}>
            {mutation.isPending ? <Spinner /> : null}
            {editing ? "保存修改" : "注册模型"}
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

type NumericField = "max_tokens" | "timeout" | "concurrency" | "retries" | "dimensions" | "top_n"

function NumberInput({
  form,
  name,
  label,
  unit,
  placeholder,
  description,
  decimal,
}: {
  form: ReturnType<typeof useForm<ModelFormValues>>
  name: NumericField
  label: string
  unit?: string
  placeholder?: string
  description?: string
  decimal?: boolean
}) {
  const error = form.formState.errors[name]
  const id = `model-form-${name}`
  return (
    <Field data-invalid={Boolean(error) || undefined}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <InputGroup>
        <InputGroupInput
          id={id}
          inputMode={decimal ? "decimal" : "numeric"}
          autoComplete="off"
          placeholder={placeholder}
          className="tabular-nums"
          aria-invalid={Boolean(error) || undefined}
          {...form.register(name)}
        />
        {unit ? (
          <InputGroupAddon align="inline-end">
            <InputGroupText>{unit}</InputGroupText>
          </InputGroupAddon>
        ) : null}
      </InputGroup>
      {error ? <FieldError errors={[error]} /> : description ? <FieldDescription className="text-xs">{description}</FieldDescription> : null}
    </Field>
  )
}
