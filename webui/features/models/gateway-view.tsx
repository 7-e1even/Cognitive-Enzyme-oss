"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ArrowRightIcon, BookOpenTextIcon, CircleAlertIcon, GhostIcon, PlusIcon, RotateCcwIcon, RouteIcon } from "lucide-react"
import Link from "next/link"
import { useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { Notice } from "@/components/notice"
import { SaveBar } from "@/components/save-bar"
import { ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@/components/ui/input-group"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Slider } from "@/components/ui/slider"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useModels, useProviders, useRoles, useSettings } from "@/lib/api/hooks"
import type { SettingItem, SettingValue } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { ChoiceBand, NoulBand } from "./confidence-band"
import { ModelListInput, ModelPicker } from "./model-picker"
import { RouteChain } from "./route-chain"

const KEYS = ["gateway.route", "gateway.low", "gateway.high", "gateway.choice", "distill.shadow_judges", "distill.shadow_rate"] as const
type Key = (typeof KEYS)[number]
type DraftValue = string | string[]

const LIST_KEYS = new Set<Key>(["gateway.route", "distill.shadow_judges"])

function toDraft(value: SettingValue | undefined, key: Key): DraftValue {
  if (LIST_KEYS.has(key)) return Array.isArray(value) ? value : []
  return value === undefined || value === null ? "" : String(value)
}

function sameValue(a: DraftValue, b: DraftValue) {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i])
  }
  return a.trim() === b.trim() || (a.trim() !== "" && b.trim() !== "" && Number(a) === Number(b))
}

function formatValue(value: SettingValue | null | undefined): string {
  if (value === null || value === undefined) return "—"
  if (Array.isArray(value)) return value.length > 0 ? value.join(" → ") : "(空)"
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

/**
 * 网关路由:System 1 网关按什么顺序问哪些判官、多有把握才算数,以及每次判定后在后台陪跑的影子判官。
 * 六个参数改完一起保存,一次提交、要么全成要么全不成。
 */
export function GatewayView() {
  const settingsQuery = useSettings()
  const modelsQuery = useModels()
  const providersQuery = useProviders()
  const rolesQuery = useRoles()
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [draft, setDraft] = useState<Partial<Record<Key, DraftValue>>>({})
  const [serverError, setServerError] = useState<{ key?: string; message: string } | null>(null)

  const specs = useMemo(() => {
    const map = new Map<string, SettingItem>()
    for (const item of settingsQuery.data?.settings ?? []) map.set(item.key, item)
    return map
  }, [settingsQuery.data])
  const models = useMemo(() => modelsQuery.data ?? [], [modelsQuery.data])
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data])
  const judgeRole = rolesQuery.data?.roles.find((r) => r.key === "model.judge")

  const server = (key: Key) => toDraft(specs.get(key)?.value, key)
  const current = (key: Key): DraftValue => (key in draft ? (draft[key] as DraftValue) : server(key))
  const dirtyKeys = KEYS.filter((key) => key in draft && !sameValue(draft[key] as DraftValue, server(key)))
  const set = (key: Key, value: DraftValue) => {
    setServerError(null)
    setDraft((prev) => ({ ...prev, [key]: value }))
  }

  const route = current("gateway.route") as string[]
  const shadow = current("distill.shadow_judges") as string[]
  const low = current("gateway.low") as string
  const high = current("gateway.high") as string
  const choice = current("gateway.choice") as string
  const rate = current("distill.shadow_rate") as string

  // 前端先按同一套取值范围校验一遍,后端还会再核
  const errors: Partial<Record<Key, string>> = {}
  const checkNumber = (key: Key, raw: string) => {
    const spec = specs.get(key)
    const n = Number(raw)
    if (raw.trim() === "") errors[key] = "请填写数值"
    else if (!Number.isFinite(n)) errors[key] = "需要是数字"
    else if (spec?.min !== null && spec?.min !== undefined && n < spec.min) errors[key] = `不能小于 ${spec.min}`
    else if (spec?.max !== null && spec?.max !== undefined && n > spec.max) errors[key] = `不能大于 ${spec.max}`
  }
  checkNumber("gateway.low", low)
  checkNumber("gateway.high", high)
  checkNumber("gateway.choice", choice)
  checkNumber("distill.shadow_rate", rate)
  if (route.length === 0) errors["gateway.route"] = "路由里至少要有一位判官,否则网关对所有请求返回 503"
  if (serverError?.key && (KEYS as readonly string[]).includes(serverError.key)) {
    errors[serverError.key as Key] = serverError.message
  }
  const invalidCount = Object.keys(errors).length

  const save = useMutation({
    mutationFn: ({ values, reset }: { values: Record<string, SettingValue>; reset: string[] }) => adminApi.updateSettings(values, reset),
    onSuccess: (snapshot, { values, reset }) => {
      queryClient.setQueryData(qk.settings, snapshot)
      void queryClient.invalidateQueries({ queryKey: qk.models })
      const keys = [...Object.keys(values), ...reset]
      setDraft((prev) => {
        const next = { ...prev }
        for (const key of keys) delete next[key as Key]
        return next
      })
      const changed = snapshot.changed ?? keys
      const labels = changed.map((key) => specs.get(key)?.label ?? key)
      const resetOnly = reset.length > 0 && Object.keys(values).length === 0
      if (changed.length === 0) toast.info("没有需要保存的变化")
      else toast.success(resetOnly ? "已恢复默认" : `已保存 ${changed.length} 项`, { description: labels.join("、") })
    },
    onError: (error) => {
      const field = error instanceof ApiError ? error.field : undefined
      setServerError({ key: field, message: errorMessage(error) })
      toast.error("保存失败", {
        description: field ? `${specs.get(field)?.label ?? field}:${errorMessage(error)}` : errorMessage(error),
      })
    },
  })

  function handleSave() {
    if (invalidCount > 0) {
      toast.error("还有不合法的取值", { description: Object.values(errors).join(";") })
      return
    }
    const values: Record<string, SettingValue> = {}
    for (const key of dirtyKeys) {
      const value = current(key)
      values[key] = Array.isArray(value) ? value : Number(value)
    }
    save.mutate({ values, reset: [] })
  }

  /** 恢复默认:删掉后台保存的值,回落到环境变量或内置默认值。立即提交,不进草稿。 */
  async function handleReset(keys: Key[]) {
    const targets = keys.map((key) => specs.get(key)).filter((spec): spec is SettingItem => spec?.source === "db")
    if (targets.length === 0) return
    const ok = await confirm({
      title: targets.length === 1 ? `把「${targets[0].label}」恢复默认?` : `把 ${targets.length} 项恢复默认?`,
      description: targets
        .map(
          (spec) =>
            `${spec.label} → ${spec.env_value !== null && spec.env_value !== undefined ? "环境变量的值" : "内置默认值"} ${formatValue(spec.env_value ?? spec.default)}`,
        )
        .join(";"),
      confirmText: "恢复默认",
    })
    if (!ok) return
    setDraft((prev) => {
      const next = { ...prev }
      for (const spec of targets) delete next[spec.key as Key]
      return next
    })
    save.mutate({ values: {}, reset: targets.map((spec) => spec.key) })
  }

  const loading = settingsQuery.isLoading || modelsQuery.isLoading
  const decisionLog = specs.get("distill.decision_log")?.value
  const lowNum = Number(low)
  const highNum = Number(high)
  const choiceNum = Number(choice)
  const rateNum = Number(rate)

  return (
    <div className="flex flex-col gap-4">
      {loading ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
          <Skeleton className="h-96" />
          <Skeleton className="h-96" />
        </div>
      ) : settingsQuery.error ? (
        <ErrorState error={settingsQuery.error} onRetry={() => void settingsQuery.refetch()} />
      ) : (
        <>
          <Card>
            <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 flex-col gap-1">
                <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  <Badge variant="secondary" className="font-mono">
                    POST /api/v1/systemone
                  </Badge>
                  TypeSafe System One 协议
                </span>
                <p className="text-sm text-muted-foreground">
                  其他 agent 按 Jev 同款协议提问;网关按下面的顺序依次询问判官,把每次调用写进判定记录,路上每位判官的作答都存为老师标注。
                </p>
              </div>
              <Button size="sm" variant="outline" asChild className="shrink-0">
                <Link href="/settings/integrations#gateway">
                  <BookOpenTextIcon /> 调用示例
                </Link>
              </Button>
            </CardContent>
          </Card>

          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)]">
            <Card>
              <CardHeader className="flex-row items-start justify-between gap-3">
                <div className="flex min-w-0 flex-col gap-1">
                  <CardTitle className="flex items-center gap-2">
                    <RouteIcon className="size-4 text-muted-foreground" aria-hidden="true" />
                    判官路由
                  </CardTitle>
                  <CardDescription>{specs.get("gateway.route")?.help}</CardDescription>
                </div>
                <div className="flex shrink-0 items-center gap-1.5">
                  <SourceMark specs={[specs.get("gateway.route")]} onReset={() => void handleReset(["gateway.route"])} />
                  <ModelPicker
                    models={models}
                    providers={providers}
                    kinds={["chat", "judge"]}
                    allowAlias
                    exclude={route}
                    align="end"
                    onPick={(id) => set("gateway.route", [...route, id])}
                    trigger={
                      <Button type="button" size="sm" variant="outline">
                        <PlusIcon /> 添加判官
                      </Button>
                    }
                  />
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {route.length > 0 ? (
                  <RouteChain route={route} onChange={(next) => set("gateway.route", next)} models={models} providers={providers} judgeRole={judgeRole} />
                ) : (
                  <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
                    <RouteIcon className="size-5" aria-hidden="true" />
                    路由是空的。添加至少一位判官——通常是「jev」(当前适用性判官)。
                  </div>
                )}
                {errors["gateway.route"] ? <FieldError>{errors["gateway.route"]}</FieldError> : null}
                <p className="text-xs text-muted-foreground">
                  判官模型走 System One 协议直接作答;对话模型以老师身份用提示词回答同样的带类型问题。请求里指定 <span className="font-mono">model</span> 时只问那一位,不走路由。
                </p>
              </CardContent>
            </Card>

            <div className="flex min-w-0 flex-col gap-4">
              <Card>
                <CardHeader>
                  <CardTitle>置信门槛</CardTitle>
                  <CardDescription>判官给出的概率落在哪里算「有把握」;任何一道题没把握,就整批升级给下一位。</CardDescription>
                </CardHeader>
                <CardContent className="flex flex-col gap-5">
                  <Field data-invalid={Boolean(errors["gateway.low"] || errors["gateway.high"]) || undefined}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <FieldLabel>是否题(noul)</FieldLabel>
                      <SourceMark
                        specs={[specs.get("gateway.low"), specs.get("gateway.high")]}
                        onReset={() => void handleReset(["gateway.low", "gateway.high"])}
                      />
                    </div>
                    <NoulBand
                      low={lowNum}
                      high={highNum}
                      lowMax={specs.get("gateway.low")?.max ?? 0.5}
                      highMin={specs.get("gateway.high")?.min ?? 0.5}
                      invalid={Boolean(errors["gateway.low"] || errors["gateway.high"])}
                      onChange={(nextLow, nextHigh) => {
                        if (String(nextLow) !== low) set("gateway.low", String(nextLow))
                        if (String(nextHigh) !== high) set("gateway.high", String(nextHigh))
                      }}
                    />
                    <div className="grid grid-cols-2 gap-3">
                      <ThresholdInput id="gateway-low" label={specs.get("gateway.low")?.label ?? "把握下限"} value={low} error={errors["gateway.low"]} onChange={(value) => set("gateway.low", value)} />
                      <ThresholdInput id="gateway-high" label={specs.get("gateway.high")?.label ?? "把握上限"} value={high} error={errors["gateway.high"]} onChange={(value) => set("gateway.high", value)} />
                    </div>
                    <FieldDescription className="text-xs">
                      {lowNum === highNum
                        ? "上下限相同:任何概率都算有把握,网关永远只问第一位。"
                        : `概率 ≤ ${low || "—"} 视为有把握的「否」,≥ ${high || "—"} 视为有把握的「是」;落在中间就升级。`}
                    </FieldDescription>
                  </Field>
                  <Separator />
                  <Field data-invalid={Boolean(errors["gateway.choice"]) || undefined}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <FieldLabel>单选题(choice)</FieldLabel>
                      <SourceMark specs={[specs.get("gateway.choice")]} onReset={() => void handleReset(["gateway.choice"])} />
                    </div>
                    <ChoiceBand value={choiceNum} invalid={Boolean(errors["gateway.choice"])} onChange={(value) => set("gateway.choice", String(value))} />
                    <div className="grid grid-cols-2 gap-3">
                      <ThresholdInput id="gateway-choice" label={specs.get("gateway.choice")?.label ?? "把握线"} value={choice} error={errors["gateway.choice"]} onChange={(value) => set("gateway.choice", value)} />
                    </div>
                    <FieldDescription className="text-xs">最高选项的概率 ≥ {choice || "—"} 视为有把握。评分题(score)不设门槛。</FieldDescription>
                  </Field>
                </CardContent>
              </Card>

              <Card>
                <CardHeader className="flex-row items-start justify-between gap-3">
                  <div className="flex min-w-0 flex-col gap-1">
                    <CardTitle className="flex items-center gap-2">
                      <GhostIcon className="size-4 text-muted-foreground" aria-hidden="true" />
                      影子判官
                    </CardTitle>
                    <CardDescription>{specs.get("distill.shadow_judges")?.help}</CardDescription>
                  </div>
                  <SourceMark specs={[specs.get("distill.shadow_judges")]} onReset={() => void handleReset(["distill.shadow_judges"])} />
                </CardHeader>
                <CardContent className="flex flex-col gap-4">
                  {decisionLog === false ? (
                    <Notice
                      tone="warning"
                      title="判定记录已关闭"
                      action={
                        <Button size="sm" variant="outline" asChild>
                          <Link href="/settings/runtime?q=distill.decision_log">
                            去开启 <ArrowRightIcon />
                          </Link>
                        </Button>
                      }
                    >
                      影子判官只在记录判定时才会作答,现在不会运行。
                    </Notice>
                  ) : null}
                  <ModelListInput
                    value={shadow}
                    onChange={(next) => set("distill.shadow_judges", next)}
                    models={models}
                    providers={providers}
                    kinds={["chat", "judge"]}
                    allowAlias
                    emptyText="没有影子判官"
                  />
                  <Field data-invalid={Boolean(errors["distill.shadow_rate"]) || undefined}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <FieldLabel htmlFor="shadow-rate">{specs.get("distill.shadow_rate")?.label ?? "影子抽样率"}</FieldLabel>
                      <SourceMark specs={[specs.get("distill.shadow_rate")]} onReset={() => void handleReset(["distill.shadow_rate"])} />
                    </div>
                    <div className="flex items-center gap-3">
                      <Slider
                        min={0}
                        max={1}
                        step={0.05}
                        value={[Number.isFinite(rateNum) ? Math.min(Math.max(rateNum, 0), 1) : 0]}
                        aria-label="影子抽样率"
                        onValueChange={(values) => set("distill.shadow_rate", String(Math.round(values[0] * 100) / 100))}
                        className="flex-1"
                      />
                      <InputGroup className="h-8 w-28 shrink-0">
                        <InputGroupInput
                          id="shadow-rate"
                          inputMode="decimal"
                          className="tabular-nums"
                          value={rate}
                          aria-invalid={Boolean(errors["distill.shadow_rate"]) || undefined}
                          onChange={(event) => set("distill.shadow_rate", event.target.value)}
                        />
                        <InputGroupAddon align="inline-end">
                          <InputGroupText>{Number.isFinite(rateNum) ? `${Math.round(rateNum * 100)}%` : ""}</InputGroupText>
                        </InputGroupAddon>
                      </InputGroup>
                    </div>
                    {errors["distill.shadow_rate"] ? (
                      <FieldError>{errors["distill.shadow_rate"]}</FieldError>
                    ) : (
                      <FieldDescription className="text-xs">
                        {specs.get("distill.shadow_rate")?.help}
                        {Number.isFinite(rateNum) && shadow.length > 0
                          ? ` 按现在的设置,每 100 次判定约有 ${Math.round(rateNum * 100)} 次交给 ${shadow.length} 位影子判官。`
                          : ""}
                      </FieldDescription>
                    )}
                  </Field>
                </CardContent>
              </Card>
            </div>
          </div>

          <SaveBar
            count={dirtyKeys.length}
            saving={save.isPending}
            note={
              invalidCount > 0 ? (
                <span className="inline-flex items-center gap-1 text-destructive">
                  <CircleAlertIcon className="size-3.5" aria-hidden="true" />
                  {invalidCount} 项取值不合法
                </span>
              ) : (
                dirtyKeys.map((key) => specs.get(key)?.label ?? key).join("、")
              )
            }
            onDiscard={() => {
              setDraft({})
              setServerError(null)
            }}
            onSave={handleSave}
          />
        </>
      )}
      {confirmDialog}
    </div>
  )
}

function ThresholdInput({
  id,
  label,
  value,
  error,
  onChange,
}: {
  id: string
  label: string
  value: string
  error?: string
  onChange: (value: string) => void
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="truncate text-xs text-muted-foreground">
        {label}
      </label>
      <InputGroup className={cn("h-8", error && "border-destructive")}>
        <InputGroupInput id={id} inputMode="decimal" className="tabular-nums" value={value} aria-invalid={Boolean(error) || undefined} onChange={(event) => onChange(event.target.value)} />
      </InputGroup>
      {error ? <span className="text-xs text-destructive">{error}</span> : null}
    </div>
  )
}

/**
 * 值从哪来:在后台保存过的给一个「恢复默认」,来自环境变量的标出来,都是默认值就什么也不显示。
 * 一组参数(是否题的上下限)共用一个标记,免得并排两个一模一样的按钮。
 */
function SourceMark({ specs, onReset }: { specs: (SettingItem | undefined)[]; onReset: () => void }) {
  const items = specs.filter((spec): spec is SettingItem => Boolean(spec))
  if (items.some((spec) => spec.source === "db")) {
    return (
      <Button
        type="button"
        size="xs"
        variant="ghost"
        className="text-muted-foreground"
        title={`${items.map((spec) => spec.label).join("、")} 在后台保存过;删除保存的值,回落到环境变量或默认值`}
        onClick={onReset}
      >
        <RotateCcwIcon /> 恢复默认
      </Button>
    )
  }
  if (items.some((spec) => spec.source === "env")) {
    const source = labelOf.source("env")
    return <ToneBadge tone={source.tone} label={source.label} title={source.hint} />
  }
  return null
}
