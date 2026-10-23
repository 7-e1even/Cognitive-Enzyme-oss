"use client"

import { useQuery, useQueryClient } from "@tanstack/react-query"
import { EraserIcon, HistoryIcon, PlayIcon } from "lucide-react"
import { useId } from "react"
import { type UseFormReturn } from "react-hook-form"
import { toast } from "sonner"
import { z } from "zod"

import { Hint } from "@/components/hint"
import { RelativeTime } from "@/components/relative-time"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Kbd } from "@/components/ui/kbd"
import { Slider } from "@/components/ui/slider"
import { Textarea } from "@/components/ui/textarea"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { errorMessage } from "@/lib/api/client"
import { adminApi, enzymeApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"

import { DEFAULT_PARAMS, excerpt, type PrimeParams } from "./data"

export const primeSchema = z.object({
  context: z.string().trim().min(1, "请输入一段情境"),
  domain: z.string().trim().max(64, "领域最多 64 个字符"),
  project: z.string().trim().max(128, "项目标识最多 128 个字符"),
  max_results: z.number({ error: "请输入 1~50 的整数" }).int("请输入整数").min(1, "至少 1 条").max(50, "最多 50 条"),
  hook_min_conf: z.number().min(0).max(1),
})

/**
 * 情境与参数。和线上 prime 用同一条流水线,但不写激活、不应期,也不记判定记录——
 * 所以可以放心把真实的情境拿来反复试。右上角能把最近的真实判定原样载入。
 */
export function PrimeForm({ form, pending, onRun }: { form: UseFormReturn<PrimeParams>; pending: boolean; onRun: (params: PrimeParams) => void }) {
  const id = useId()
  const queryClient = useQueryClient()
  const facets = useQuery({ queryKey: qk.facets, queryFn: enzymeApi.facets, staleTime: 60_000 })
  const recent = useQuery({
    queryKey: qk.decisions({ limit: 8, offset: 0, recent: true }),
    queryFn: () => adminApi.decisions({ limit: 8, offset: 0 }),
    staleTime: 30_000,
  })
  const domains = Object.keys(facets.data?.domain ?? {}).filter(Boolean)
  const { register, watch, setValue, handleSubmit, formState, reset } = form
  const submit = handleSubmit((values) => onRun(values))
  const hookMinConf = watch("hook_min_conf")

  // 列表里的情境截断过,载入时取完整的那条
  const loadDecision = async (decisionId: string) => {
    try {
      const rec = await queryClient.fetchQuery({ queryKey: ["admin", "decision", decisionId], queryFn: () => adminApi.decision(decisionId) })
      reset({ ...form.getValues(), context: rec.request?.state ?? "", domain: rec.domain ?? "", project: rec.project ?? "" })
      toast.success("已载入这条判定的情境", { description: "参数保留原值,可以直接运行对比线上结果。" })
    } catch (error) {
      toast.error(errorMessage(error))
    }
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-1.5">
          情境
          <Hint label="情境">
            粘贴智能体收到的一段提示词或当前任务描述。调试台和线上 prime 走同一条流水线(召回 → 条件 → 精排 → 打分 → 判官 → 注入),但不写激活、不触发不应期、也不记判定记录。
          </Hint>
        </CardTitle>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="xs" disabled={!recent.data?.items.length}>
              <HistoryIcon /> 最近的真实情境
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-96">
            <DropdownMenuGroup>
              <DropdownMenuLabel>从最近的判定记录载入</DropdownMenuLabel>
              {(recent.data?.items ?? []).map((row) => (
                <DropdownMenuItem key={row.id} onClick={() => void loadDecision(row.id)} className="flex-col items-start gap-0.5">
                  <span className="line-clamp-2 text-sm">{excerpt(row.state, 90) || "(没有情境文本)"}</span>
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <RelativeTime value={row.created_at} />
                    {row.project ? <span>· {row.project}</span> : null}
                    <span>· 注入 {row.injected}</span>
                  </span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </CardHeader>
      <CardContent>
        <form onSubmit={(event) => void submit(event)} className="@container grid grid-cols-1 gap-5 @3xl:grid-cols-[minmax(0,1fr)_17rem]" noValidate>
          <Field data-invalid={Boolean(formState.errors.context) || undefined} className="gap-2">
            <FieldLabel htmlFor={`${id}-context`} className="sr-only">
              情境
            </FieldLabel>
            <Textarea
              id={`${id}-context`}
              placeholder="例如:帮我把这个 dict 存进 postgres 的 jsonb 列,用的是 asyncpg,现在报 DataError"
              className="min-h-40"
              aria-invalid={Boolean(formState.errors.context) || undefined}
              {...register("context")}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault()
                  void submit()
                }
              }}
            />
            {formState.errors.context ? (
              <FieldError>{formState.errors.context.message}</FieldError>
            ) : (
              <FieldDescription className="text-xs">
                <Kbd>⌘</Kbd> <Kbd>Enter</Kbd> 运行。情境原样作为 prime 的 context,与 hook 发送的内容一致。
              </FieldDescription>
            )}
          </Field>

          <FieldGroup className="gap-4">
            <Field data-invalid={Boolean(formState.errors.domain) || undefined} className="gap-1.5">
              <FieldLabel htmlFor={`${id}-domain`}>领域</FieldLabel>
              <Input id={`${id}-domain`} placeholder="不限" list={`${id}-domains`} autoComplete="off" aria-invalid={Boolean(formState.errors.domain) || undefined} {...register("domain")} />
              <datalist id={`${id}-domains`}>
                {domains.map((domain) => (
                  <option key={domain} value={domain} />
                ))}
              </datalist>
              <FieldError>{formState.errors.domain?.message}</FieldError>
            </Field>

            <Field data-invalid={Boolean(formState.errors.project) || undefined} className="gap-1.5">
              <FieldLabel htmlFor={`${id}-project`}>项目</FieldLabel>
              <Input id={`${id}-project`} placeholder="例如 webui" autoComplete="off" aria-invalid={Boolean(formState.errors.project) || undefined} {...register("project")} />
              <FieldError>{formState.errors.project?.message}</FieldError>
            </Field>

            <Field data-invalid={Boolean(formState.errors.max_results) || undefined} className="gap-1.5">
              <FieldLabel htmlFor={`${id}-max`}>最多注入</FieldLabel>
              <Input
                id={`${id}-max`}
                type="number"
                inputMode="numeric"
                min={1}
                max={50}
                step={1}
                aria-invalid={Boolean(formState.errors.max_results) || undefined}
                {...register("max_results", { valueAsNumber: true })}
              />
              <FieldError>{formState.errors.max_results?.message}</FieldError>
            </Field>

            <Field className="gap-2">
              <div className="flex items-center justify-between gap-2">
                <FieldLabel htmlFor={`${id}-conf`}>hook 置信度下限</FieldLabel>
                <span className="text-sm text-muted-foreground tabular-nums">{hookMinConf.toFixed(2)}</span>
              </div>
              <Slider
                id={`${id}-conf`}
                min={0}
                max={1}
                step={0.05}
                value={[hookMinConf]}
                onValueChange={(value) => setValue("hook_min_conf", value[0] ?? 0, { shouldDirty: true })}
                aria-label="hook 置信度下限"
              />
              <FieldDescription className="text-xs">Claude Code 的 hook 会丢掉置信度低于它的注入(探索项豁免),表格里会标出。</FieldDescription>
            </Field>
          </FieldGroup>

          <div className="flex flex-wrap items-center justify-end gap-2 @3xl:col-span-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => reset(DEFAULT_PARAMS)} disabled={pending}>
              <EraserIcon /> 清空
            </Button>
            <Button type="submit" size="sm" disabled={pending}>
              {pending ? <Spinner /> : <PlayIcon />}
              {pending ? "运行中…" : "运行 prime"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
