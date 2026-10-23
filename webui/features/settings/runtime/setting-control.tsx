"use client"

import { WandSparklesIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@/components/ui/input-group"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { ModelEntry, Provider, SettingItem } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import { ModelListInput } from "@/features/models/model-picker"

import { stepFor, type DraftValue } from "../lib/settings"
import { TagInput } from "./tag-input"

const NO_MODEL = "__none__"

/** 枚举选项少、字又短时用分段按钮,一眼看全;否则收进下拉。 */
export function prefersToggle(item: SettingItem) {
  return item.choices.length <= 4 && item.choices.every((c) => c.label.length <= 10)
}

/** 按类型渲染一个运行参数的控件。值一律是草稿形态(数字是文本),半截输入不会被格式化掉,提交前再统一校验。 */
export function SettingControl({
  id,
  item,
  value,
  onChange,
  disabled,
  invalid,
  models,
  providers,
}: {
  id: string
  item: SettingItem
  value: DraftValue
  onChange: (value: DraftValue) => void
  disabled?: boolean
  invalid?: boolean
  models: ModelEntry[]
  providers: Provider[]
}) {
  switch (item.type) {
    case "bool":
      return <Switch id={id} checked={Boolean(value)} disabled={disabled} aria-label={item.label} onCheckedChange={(checked) => onChange(checked)} />
    case "enum":
      if (prefersToggle(item)) {
        return (
          <ToggleGroup
            id={id}
            type="single"
            variant="outline"
            size="sm"
            spacing={0}
            disabled={disabled}
            value={String(value)}
            onValueChange={(next) => {
              if (next) onChange(next)
            }}
            aria-label={item.label}
            className="flex-wrap"
          >
            {item.choices.map((choice) => (
              <ToggleGroupItem key={choice.value} value={choice.value} title={choice.value}>
                {choice.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        )
      }
      return (
        <Select value={String(value)} disabled={disabled} onValueChange={(next) => next && onChange(next)}>
          <SelectTrigger id={id} className="w-full" aria-invalid={invalid || undefined} aria-label={item.label}>
            <SelectValue>{item.choices.find((c) => c.value === value)?.label ?? String(value)}</SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {item.choices.map((choice) => (
                <SelectItem key={choice.value} value={choice.value}>
                  {choice.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      )
    case "int":
    case "float":
      return (
        <InputGroup className={cn(invalid && "border-destructive")}>
          <InputGroupInput
            id={id}
            type="number"
            inputMode={item.type === "int" ? "numeric" : "decimal"}
            step={stepFor(item)}
            min={item.min ?? undefined}
            max={item.max ?? undefined}
            disabled={disabled}
            aria-invalid={invalid || undefined}
            className="tabular-nums"
            value={String(value)}
            onChange={(event) => onChange(event.target.value)}
          />
          {item.unit ? (
            <InputGroupAddon align="inline-end">
              <InputGroupText>{item.unit}</InputGroupText>
            </InputGroupAddon>
          ) : null}
        </InputGroup>
      )
    case "list":
      return <TagInput id={id} value={Array.isArray(value) ? value : []} disabled={disabled} invalid={invalid} onChange={(next) => onChange(next)} />
    case "models":
      return (
        <div className={cn(disabled && "pointer-events-none opacity-50")}>
          <ModelListInput
            value={Array.isArray(value) ? value : []}
            onChange={(next) => onChange(next)}
            models={models}
            providers={providers}
            kinds={item.kinds.length > 0 ? item.kinds : undefined}
            allowAlias={item.kinds.includes("judge")}
            invalid={invalid}
            emptyText="(空)"
          />
        </div>
      )
    case "model": {
      const candidates = models.filter((m) => item.kinds.length === 0 || item.kinds.includes(m.kind))
      return (
        <Select value={String(value) || NO_MODEL} disabled={disabled} onValueChange={(next) => onChange(next === NO_MODEL ? "" : next)}>
          <SelectTrigger id={id} className="w-full" aria-invalid={invalid || undefined} aria-label={item.label}>
            <SelectValue>
              {value ? <span className="font-mono">{String(value)}</span> : <span className="text-muted-foreground">不设置</span>}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value={NO_MODEL}>不设置</SelectItem>
              {candidates.map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  <span className="font-mono">{m.id}</span>
                  <span className="text-xs text-muted-foreground">{m.provider_name}</span>
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      )
    }
    case "json":
      return (
        <div className="flex flex-col gap-1.5">
          <Textarea
            id={id}
            rows={4}
            spellCheck={false}
            disabled={disabled}
            aria-invalid={invalid || undefined}
            className="font-mono text-xs"
            placeholder="{}"
            value={String(value)}
            onChange={(event) => onChange(event.target.value)}
          />
          <Button
            type="button"
            variant="ghost"
            size="xs"
            className="self-end"
            disabled={disabled}
            onClick={() => {
              try {
                const raw = String(value).trim()
                if (raw) onChange(JSON.stringify(JSON.parse(raw), null, 2))
              } catch {
                /* 格式错误时不动,错误信息已经显示在下面 */
              }
            }}
          >
            <WandSparklesIcon /> 格式化
          </Button>
        </div>
      )
    default:
      return (
        <Input
          id={id}
          disabled={disabled}
          aria-invalid={invalid || undefined}
          spellCheck={false}
          autoComplete="off"
          className="font-mono text-[13px]"
          placeholder={item.default ? String(item.default) : "留空"}
          value={String(value)}
          onChange={(event) => onChange(event.target.value)}
        />
      )
  }
}
