"use client"

import { ChevronRightIcon, RotateCcwIcon, Undo2Icon } from "lucide-react"
import { useState } from "react"

import { ToneBadge } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { ModelEntry, Provider, SettingItem } from "@/lib/api/types"
import { formatDateTime, formatRelative } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { fallbackValue, formatSettingValue, rangeHint, type DraftValue } from "../lib/settings"
import { SettingControl } from "./setting-control"

/**
 * 一个运行参数:左栏名称 + 标签 + 一行说明,右栏固定宽度放控件。
 * 来源标签只有值不是默认时才出现;键名和环境变量名收在「高级」折叠里;
 * 改过但没保存的行左边一道竖线,错误的行控件标红并写出原因。
 */
export function SettingRow({
  item,
  value,
  dirty,
  pendingReset,
  error,
  models,
  providers,
  onChange,
  onRevert,
  onReset,
  onCancelReset,
}: {
  item: SettingItem
  value: DraftValue
  dirty: boolean
  pendingReset: boolean
  error?: string
  models: ModelEntry[]
  providers: Provider[]
  onChange: (value: DraftValue) => void
  onRevert: () => void
  onReset: () => void
  onCancelReset: () => void
}) {
  const source = labelOf.source(item.source)
  const controlId = `setting-${item.key}`
  const fallback = fallbackValue(item)
  const range = rangeHint(item)
  const wide = item.type === "json" || item.type === "list" || item.type === "models"
  const [detailOpen, setDetailOpen] = useState(false)

  return (
    <Field
      id={`row-${item.key}`}
      data-row-id={item.key}
      orientation="responsive"
      className={cn(
        "relative scroll-mt-24 gap-4 px-4 py-4 transition-colors",
        (dirty || pendingReset) && "bg-primary/[0.035]",
        error && "bg-destructive/[0.04]",
      )}
    >
      {dirty || pendingReset || error ? (
        <span className={cn("absolute inset-y-2 start-0 w-0.5 rounded-full", error ? "bg-destructive" : "bg-primary")} aria-hidden="true" />
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col gap-0.5 @md/field-group:max-w-md">
        <div className="flex flex-wrap items-center gap-2">
          <FieldLabel htmlFor={controlId}>{item.label}</FieldLabel>
          {item.source !== "default" ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex">
                  <ToneBadge tone={source.tone} label={source.label} />
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-64">
                {item.source === "db" && item.updated_at
                  ? `在后台保存于 ${formatDateTime(item.updated_at)}(${formatRelative(item.updated_at)})`
                  : source.hint}
              </TooltipContent>
            </Tooltip>
          ) : null}
          {item.restart ? <ToneBadge tone="warning-light" label="需重启" title="改动在服务重启后才生效" /> : null}
          {item.advanced ? <Badge variant="outline">高级</Badge> : null}
          {dirty ? <ToneBadge tone="primary-light" label="已修改" /> : null}
          {pendingReset ? <ToneBadge tone="primary-light" label="待恢复默认" /> : null}
        </div>
        {item.help ? <FieldDescription className="text-xs leading-relaxed">{item.help}</FieldDescription> : null}
        {item.source === "env" ? (
          <p className="text-xs text-amber-700 dark:text-amber-400">
            来自环境变量 <span className="font-mono">{item.env}</span> ={" "}
            <span className="font-mono break-all">{formatSettingValue(item, item.env_value)}</span>;在这里保存后以后台为准。
          </p>
        ) : null}
        {item.source === "db" && item.env_value !== null && item.env_value !== undefined && !pendingReset ? (
          <p className="text-xs text-muted-foreground">
            覆盖了环境变量 <span className="font-mono">{item.env}</span> = <span className="font-mono break-all">{formatSettingValue(item, item.env_value)}</span>
          </p>
        ) : null}
        {pendingReset ? (
          <p className="text-xs text-primary">
            保存后删除后台值,回到{fallback.from === "env" ? "环境变量" : "默认值"}{" "}
            <span className="font-mono break-all">{formatSettingValue(item, fallback.value)}</span>
          </p>
        ) : null}
        <Collapsible open={detailOpen} onOpenChange={setDetailOpen}>
          <CollapsibleTrigger className="mt-0.5 inline-flex items-center gap-1 rounded-sm text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50">
            <ChevronRightIcon className={cn("size-3 transition-transform", detailOpen && "rotate-90")} aria-hidden="true" />
            高级
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-1 flex flex-col gap-0.5 font-mono text-xs text-muted-foreground">
            <span>{item.key}</span>
            {item.env ? <span className="opacity-70">{item.env}</span> : null}
          </CollapsibleContent>
        </Collapsible>
      </div>

      <FieldContent className={cn("min-w-0 gap-1.5", wide ? "@md/field-group:w-[26rem]" : "@md/field-group:w-80")}>
        <div className={cn("flex min-w-0 items-start gap-1.5", item.type === "bool" && "justify-end")}>
          <div className={cn("min-w-0", item.type === "bool" ? "flex h-8 items-center" : "flex-1")}>
            <SettingControl id={controlId} item={item} value={value} onChange={onChange} disabled={pendingReset} invalid={Boolean(error)} models={models} providers={providers} />
          </div>
          <div className="flex size-8 shrink-0 items-center justify-center">
            {pendingReset ? (
              <IconAction label="撤销恢复默认" onClick={onCancelReset} />
            ) : dirty ? (
              <IconAction label="撤销修改" onClick={onRevert} />
            ) : null}
          </div>
        </div>
        {error ? (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        ) : range || item.source !== "default" ? (
          <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
            {range ? <span>范围 {range}</span> : null}
            {range && item.source !== "default" ? <span aria-hidden="true">·</span> : null}
            {item.source !== "default" ? <span>默认 {formatSettingValue(item, item.default)}</span> : null}
            {item.source === "db" && !dirty && !pendingReset ? (
              <>
                <span aria-hidden="true">·</span>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded-sm font-medium text-foreground underline-offset-4 outline-none hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring/50"
                  onClick={onReset}
                >
                  <RotateCcwIcon className="size-3" aria-hidden="true" />
                  恢复默认
                </button>
              </>
            ) : null}
          </p>
        ) : null}
      </FieldContent>
    </Field>
  )
}

function IconAction({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={label} onClick={onClick}>
          <Undo2Icon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
