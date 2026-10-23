import { ToneBadge } from "@/components/tone-badge"
import { Progress } from "@/components/ui/progress"
import type { PatternItem } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { patternText } from "../lib/patterns"

export function SeverityBadge({ severity }: { severity: string }) {
  const meta = labelOf.severity(severity)
  return <ToneBadge tone={meta.tone} label={meta.label} title={meta.hint} />
}

export function StatusBadge({ status }: { status: string }) {
  const meta = labelOf.status(status)
  return <ToneBadge tone={meta.tone} label={meta.label} title={meta.hint} />
}

export function OriginBadge({ origin }: { origin: string }) {
  const meta = labelOf.origin(origin)
  return <ToneBadge tone={meta.tone} label={meta.label} />
}

export function DomainLabel({ domain, className }: { domain: string; className?: string }) {
  return domain ? (
    <span className={cn("truncate", className)}>{domain}</span>
  ) : (
    <span className={cn("truncate text-muted-foreground", className)}>未分类</span>
  )
}

/** 置信度条:低于 0.4 的记忆在召回里基本拿不到名额,用警示色。 */
export function ConfidenceMeter({ value, className }: { value: number; className?: string }) {
  const tone =
    value >= 0.7
      ? "**:data-[slot=progress-indicator]:bg-emerald-500"
      : value >= 0.4
        ? "**:data-[slot=progress-indicator]:bg-primary/70"
        : "**:data-[slot=progress-indicator]:bg-amber-500"
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <Progress value={Math.round(Math.max(0, Math.min(1, value)) * 100)} aria-label="置信度" className={cn("h-1.5 w-16", tone)} />
      <span className="w-9 text-right text-xs tabular-nums">{value.toFixed(2)}</span>
    </span>
  )
}

/** 质量门评分(0~5)。0 分表示从未经过质量门(手动创建的记忆)。 */
export function QualityScore({ value }: { value: number }) {
  if (!value) return <span className="text-xs text-muted-foreground">未评估</span>
  return <span className="text-xs tabular-nums">{value.toFixed(1)}</span>
}

/** 一行触发条件:关键词与正则分别用不同底色。 */
export function PatternChips({ patterns, max = 4, className }: { patterns: PatternItem[]; max?: number; className?: string }) {
  const shown = patterns.slice(0, max)
  const rest = patterns.length - shown.length
  return (
    <span className={cn("flex min-w-0 items-center gap-1 overflow-hidden text-xs text-muted-foreground", className)}>
      {shown.map((item, index) => (
        <span
          key={`${item.type}-${item.value}-${index}`}
          className={cn(
            "max-w-40 shrink-0 truncate rounded-md px-1 font-mono text-[11px] leading-4",
            item.type === "regex" ? "bg-sky-500/15 text-sky-700 dark:text-sky-400" : "bg-muted",
          )}
        >
          {patternText(item)}
        </span>
      ))}
      {rest > 0 ? <span className="shrink-0 tabular-nums">+{rest}</span> : null}
    </span>
  )
}
