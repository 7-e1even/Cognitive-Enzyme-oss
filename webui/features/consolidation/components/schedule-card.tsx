"use client"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldContent, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { OptionSelect } from "@/components/option-select"
import { formatInterval } from "@/lib/format"

import { draftInterval, INTERVAL_UNITS, type IntervalUnit, type ScheduleDraft } from "../lib/consolidation"

const PRESETS = [
  { seconds: 3600, amount: 1, unit: "hour" as const },
  { seconds: 6 * 3600, amount: 6, unit: "hour" as const },
  { seconds: 12 * 3600, amount: 12, unit: "hour" as const },
  { seconds: 86400, amount: 1, unit: "day" as const },
]

/** 自动调度:开关 + 间隔(数字 + 单位,四个常用间隔一键选)。 */
export function ScheduleCard({
  draft,
  onChange,
  error,
  disabled,
}: {
  draft: ScheduleDraft
  onChange: <K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) => void
  error: string | null
  disabled: boolean
}) {
  const interval = draftInterval(draft)
  const preset = PRESETS.find((item) => item.seconds === interval)

  return (
    <Card>
      <CardHeader>
        <CardTitle>自动调度</CardTitle>
        <CardDescription>{draft.enabled && interval && interval >= 60 ? `每 ${formatInterval(interval)} 排队一次` : "只在手动执行时巩固"}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <Field orientation="horizontal">
          <FieldContent>
            <FieldLabel htmlFor="schedule-enabled">自动巩固</FieldLabel>
            <FieldDescription className="text-xs">关掉后只能在这一页手动执行。</FieldDescription>
          </FieldContent>
          <Switch id="schedule-enabled" checked={draft.enabled} disabled={disabled} onCheckedChange={(checked) => onChange("enabled", checked)} />
        </Field>

        <Field data-invalid={Boolean(error) || undefined} data-disabled={!draft.enabled || undefined}>
          <FieldLabel htmlFor="schedule-interval">间隔</FieldLabel>
          <div className="flex items-center gap-2">
            <Input
              id="schedule-interval"
              type="number"
              min={1}
              step={1}
              inputMode="decimal"
              value={draft.intervalAmount ?? ""}
              disabled={disabled}
              aria-invalid={Boolean(error) || undefined}
              className="flex-1 tabular-nums"
              onChange={(event) => onChange("intervalAmount", event.target.value === "" ? null : Number(event.target.value))}
            />
            <OptionSelect
              size="default"
              label="间隔单位"
              value={draft.intervalUnit}
              options={INTERVAL_UNITS.map((unit) => ({ value: unit.value, label: unit.label }))}
              disabled={disabled}
              onChange={(value) => value && onChange("intervalUnit", value as IntervalUnit)}
              className="w-24"
            />
          </div>
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={0}
            value={preset ? String(preset.seconds) : ""}
            disabled={disabled}
            aria-label="常用间隔"
            onValueChange={(value) => {
              const picked = PRESETS.find((item) => String(item.seconds) === value)
              if (!picked) return
              onChange("intervalAmount", picked.amount)
              onChange("intervalUnit", picked.unit)
            }}
          >
            {PRESETS.map((item) => (
              <ToggleGroupItem key={item.seconds} value={String(item.seconds)} className="px-3 text-xs">
                {formatInterval(item.seconds)}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          {error ? (
            <FieldError>{error}</FieldError>
          ) : (
            <FieldDescription className="text-xs">服务启动 1 分钟后排第一次,之后每隔这么久排一次。改了间隔,要等当前这一轮等完才按新的计时。</FieldDescription>
          )}
        </Field>
      </CardContent>
    </Card>
  )
}
