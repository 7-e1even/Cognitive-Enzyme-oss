"use client"

import { Slider } from "@/components/ui/slider"
import { cn } from "@/lib/utils"

const pct = (value: number) => `${Math.round(value * 1000) / 10}%`
const round = (value: number) => Math.round(value * 100) / 100

/**
 * 是否题的置信带:0 ~ 1 的概率轴上三段——有把握的「否」、没把握(升级)、有把握的「是」。
 * 两个滑块就是两条门槛;下限不过 0.5、上限不低于 0.5(与后端的取值范围一致)。
 */
export function NoulBand({
  low,
  high,
  onChange,
  lowMax = 0.5,
  highMin = 0.5,
  invalid,
}: {
  low: number
  high: number
  onChange: (low: number, high: number) => void
  lowMax?: number
  highMin?: number
  invalid?: boolean
}) {
  const safeLow = Number.isFinite(low) ? Math.min(Math.max(low, 0), 1) : 0
  const safeHigh = Number.isFinite(high) ? Math.min(Math.max(high, 0), 1) : 1
  return (
    <div className="flex flex-col gap-3">
      <div className={cn("relative flex h-9 overflow-hidden rounded-lg border text-[11px] font-medium", invalid && "border-destructive")} aria-hidden="true">
        <div
          className="flex items-center justify-center overflow-hidden bg-emerald-500/15 px-1 whitespace-nowrap text-emerald-700 dark:text-emerald-400"
          style={{ width: pct(safeLow) }}
        >
          {safeLow >= 0.08 ? "有把握:否" : null}
        </div>
        <div
          className="flex flex-1 items-center justify-center overflow-hidden bg-amber-500/15 px-1 whitespace-nowrap text-amber-700 dark:text-amber-400"
          style={{ width: pct(Math.max(0, safeHigh - safeLow)) }}
        >
          没把握 → 升级给下一位
        </div>
        <div
          className="flex items-center justify-center overflow-hidden bg-emerald-500/15 px-1 whitespace-nowrap text-emerald-700 dark:text-emerald-400"
          style={{ width: pct(Math.max(0, 1 - safeHigh)) }}
        >
          {1 - safeHigh >= 0.08 ? "有把握:是" : null}
        </div>
      </div>
      <Slider
        min={0}
        max={1}
        step={0.01}
        value={[safeLow, safeHigh]}
        minStepsBetweenThumbs={1}
        aria-label="是否题的把握下限与上限"
        onValueChange={(values) => {
          const [nextLow, nextHigh] = values
          onChange(round(Math.min(nextLow, lowMax)), round(Math.max(nextHigh, highMin)))
        }}
      />
      <div className="flex justify-between text-[11px] text-muted-foreground tabular-nums" aria-hidden="true">
        <span>0</span>
        <span>0.25</span>
        <span>0.5</span>
        <span>0.75</span>
        <span>1</span>
      </div>
    </div>
  )
}

/** 单选题:最高选项的概率达到这条线才算有把握。 */
export function ChoiceBand({
  value,
  onChange,
  invalid,
}: {
  value: number
  onChange: (value: number) => void
  invalid?: boolean
}) {
  const safe = Number.isFinite(value) ? Math.min(Math.max(value, 0), 1) : 0
  return (
    <div className="flex flex-col gap-3">
      <div className={cn("relative flex h-9 overflow-hidden rounded-lg border text-[11px] font-medium", invalid && "border-destructive")} aria-hidden="true">
        <div className="flex items-center justify-center overflow-hidden bg-amber-500/15 px-1 whitespace-nowrap text-amber-700 dark:text-amber-400" style={{ width: pct(safe) }}>
          {safe >= 0.15 ? "没把握 → 升级" : null}
        </div>
        <div className="flex flex-1 items-center justify-center overflow-hidden bg-emerald-500/15 px-1 whitespace-nowrap text-emerald-700 dark:text-emerald-400">
          {1 - safe >= 0.15 ? "有把握" : null}
        </div>
      </div>
      <Slider min={0} max={1} step={0.01} value={[safe]} aria-label="单选题的把握线" onValueChange={(values) => onChange(round(values[0]))} />
    </div>
  )
}
