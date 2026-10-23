"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useState } from "react"
import { Controller, useForm } from "react-hook-form"
import { toast } from "sonner"

import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { Severity } from "@/lib/api/types"
import { SEVERITY } from "@/lib/labels"

import { SEVERITY_VALUES } from "../lib/options"
import { useCreateEnzyme } from "../lib/queries"
import { enzymeCreateSchema, type EnzymeCreateInput, type EnzymeCreateValues } from "../lib/schema"
import { DomainInput, ListEditor, PatternEditor } from "./editors"

function blankDraft(): EnzymeCreateInput {
  return { patterns: [], then: "", because: "", domain: "", tags: [], references: [], severity: "info" }
}

/** 新建表单里严重度从低到高排。 */
const SEVERITY_ORDER = [...SEVERITY_VALUES].reverse()

const SEVERITY_NOTE: Record<Severity, string> = {
  critical: "危急级永远放行,不受剂量与不应期约束,只留给真正会出事故的情况。",
  warning: "警告级:忽略它通常会造成缺陷或返工。",
  info: "提示级:最佳实践与习惯,按相关度参与召回。",
}

export function EnzymeFormSheet({
  open,
  onOpenChange,
  domains,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 已有领域 → 条数,给领域输入框做候选 */
  domains: Record<string, number>
  onCreated: (id: string) => void
}) {
  const create = useCreateEnzyme()
  const form = useForm<EnzymeCreateInput, unknown, EnzymeCreateValues>({
    resolver: zodResolver(enzymeCreateSchema),
    defaultValues: blankDraft(),
    mode: "onTouched",
  })
  const errors = form.formState.errors
  const [seenOpen, setSeenOpen] = useState(open)

  // 每次打开都是一张空表;在渲染里对齐,关闭动画期间表单保持原样
  if (seenOpen !== open) {
    setSeenOpen(open)
    if (open) {
      form.reset(blankDraft())
      create.reset()
    }
  }

  const submit = form.handleSubmit(async (values) => {
    try {
      const created = await create.mutateAsync({
        patterns: values.patterns,
        then: values.then,
        because: values.because,
        domain: values.domain,
        tags: values.tags,
        references: values.references,
        severity: values.severity,
      })
      toast.success("已新建记忆", { description: values.then })
      onOpenChange(false)
      onCreated(created.id)
    } catch {
      // 失败原因由接口错误提示给出,表单保持原样方便重试
    }
  })

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg">
        <SheetHeader className="gap-1 border-b">
          <SheetTitle>新建记忆</SheetTitle>
          <SheetDescription>手动写下的记忆直接启用,初始置信度 0.5,之后随每次反馈自动调整。</SheetDescription>
        </SheetHeader>

        <form id="enzyme-create-form" noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col overflow-y-auto px-4 py-4">
          <FieldGroup className="gap-5">
            <Controller
              control={form.control}
              name="patterns"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid || undefined}>
                  <FieldLabel htmlFor="enzyme-create-patterns">触发条件</FieldLabel>
                  <PatternEditor
                    id="enzyme-create-patterns"
                    value={field.value}
                    invalid={fieldState.invalid}
                    onChange={(next) => {
                      field.onChange(next)
                      field.onBlur()
                    }}
                  />
                  <FieldDescription>情境里出现关键词(不区分大小写)或匹配正则(Python re 语法)时,这条记忆进入召回候选。</FieldDescription>
                  <FieldError errors={[errors.patterns, ...(Array.isArray(errors.patterns) ? errors.patterns : [])]} />
                </Field>
              )}
            />

            <Field data-invalid={Boolean(errors.then) || undefined}>
              <FieldLabel htmlFor="enzyme-create-then">建议</FieldLabel>
              <Textarea
                id="enzyme-create-then"
                rows={3}
                placeholder="例如:用 datetime.now(timezone.utc) 替代 datetime.utcnow()"
                aria-invalid={Boolean(errors.then) || undefined}
                {...form.register("then")}
              />
              <FieldDescription>召回时注入给智能体的那句话,写成可以直接照做的动作。</FieldDescription>
              <FieldError errors={[errors.then]} />
            </Field>

            <Field data-invalid={Boolean(errors.because) || undefined}>
              <FieldLabel htmlFor="enzyme-create-because">原因</FieldLabel>
              <Textarea
                id="enzyme-create-because"
                rows={2}
                placeholder="为什么要这样做:踩过的坑、文档依据、失败的代价"
                aria-invalid={Boolean(errors.because) || undefined}
                {...form.register("because")}
              />
              <FieldError errors={[errors.because]} />
            </Field>

            <Controller
              control={form.control}
              name="domain"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid || undefined}>
                  <FieldLabel htmlFor="enzyme-create-domain">领域</FieldLabel>
                  <DomainInput id="enzyme-create-domain" value={field.value} domains={domains} invalid={fieldState.invalid} onChange={field.onChange} />
                  <FieldDescription>召回时可以按领域收窄范围;留空表示通用。</FieldDescription>
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="severity"
              render={({ field }) => (
                <Field>
                  <FieldLabel id="enzyme-create-severity-label">严重度</FieldLabel>
                  <ToggleGroup
                    type="single"
                    aria-labelledby="enzyme-create-severity-label"
                    variant="outline"
                    size="sm"
                    spacing={0}
                    value={field.value}
                    onValueChange={(next) => {
                      if (next) field.onChange(next as Severity)
                    }}
                  >
                    {SEVERITY_ORDER.map((value) => (
                      <ToggleGroupItem key={value} value={value} className="px-4">
                        {SEVERITY[value].label}
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                  <FieldDescription>{SEVERITY_NOTE[field.value]}</FieldDescription>
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="tags"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid || undefined}>
                  <FieldLabel htmlFor="enzyme-create-tags">标签</FieldLabel>
                  <ListEditor
                    id="enzyme-create-tags"
                    label="添加标签"
                    splitOnComma
                    placeholder="输入标签,回车或逗号添加"
                    value={field.value}
                    invalid={fieldState.invalid}
                    onChange={field.onChange}
                  />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />

            <Controller
              control={form.control}
              name="references"
              render={({ field, fieldState }) => (
                <Field data-invalid={fieldState.invalid || undefined}>
                  <FieldLabel htmlFor="enzyme-create-references">参考资料</FieldLabel>
                  <ListEditor
                    id="enzyme-create-references"
                    label="添加参考资料"
                    layout="lines"
                    placeholder="文档链接或出处,回车添加"
                    value={field.value}
                    invalid={fieldState.invalid}
                    onChange={field.onChange}
                  />
                  <FieldError errors={[fieldState.error]} />
                </Field>
              )}
            />
          </FieldGroup>
        </form>

        <Separator />
        <SheetFooter className="flex-row justify-end">
          <Button type="button" size="sm" variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button type="submit" form="enzyme-create-form" size="sm" disabled={create.isPending}>
            {create.isPending ? <Spinner /> : null}
            新建
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
