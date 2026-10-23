import type { ReactNode } from "react"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { formatNumber } from "@/lib/format"

import { ratio } from "./data"

export interface Segment {
  key: string
  label: string
  count: number
  /** 任意 CSS 颜色(语义色用 var(--...) 或 tailwind 色板变量) */
  color: string
  hint?: string
}

/** 分段条:一个整体分成几份。总数为 0 时不画(没有数就不占位置)。 */
export function SegmentedBar({ title, segments, action, unit = "条" }: { title: ReactNode; segments: Segment[]; action?: ReactNode; unit?: string }) {
  const total = segments.reduce((sum, segment) => sum + segment.count, 0)
  if (total === 0) return null
  return (
    <section className="flex flex-col gap-2.5">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-medium text-muted-foreground">{title}</h4>
        {action}
      </div>
      <div className="flex h-2 gap-0.5" role="img" aria-label={segments.map((s) => `${s.label} ${s.count}`).join(",")}>
        {segments
          .filter((segment) => segment.count > 0)
          .map((segment) => (
            <Tooltip key={segment.key}>
              <TooltipTrigger asChild>
                <span className="h-full min-w-1 rounded-full transition-opacity hover:opacity-80" style={{ flexGrow: segment.count, flexBasis: 0, backgroundColor: segment.color }} />
              </TooltipTrigger>
              <TooltipContent>
                {segment.label} {formatNumber(segment.count)} {unit} · {ratio(segment.count / total)}
              </TooltipContent>
            </Tooltip>
          ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5">
        {segments.map((segment) => (
          <li key={segment.key} className="flex items-center gap-1.5 text-xs" title={segment.hint}>
            <span className="size-2 shrink-0 rounded-sm" style={{ backgroundColor: segment.color }} aria-hidden="true" />
            <span className="text-muted-foreground">{segment.label}</span>
            <span className="font-medium tabular-nums">{formatNumber(segment.count)}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** 横条列表:谁多谁少。只用一种颜色——类别没有顺序,逐条上色只会把长度再编码一遍。 */
export function BarList({ rows }: { rows: { key: string; label: string; count: number }[] }) {
  const max = rows.reduce((m, row) => Math.max(m, row.count), 0)
  return (
    <ul className="flex flex-col gap-2.5">
      {rows.map((row) => (
        <li key={row.key} className="grid grid-cols-[6.5rem_1fr_2.5rem] items-center gap-3 text-xs">
          <span className="truncate text-muted-foreground" title={row.label}>
            {row.label}
          </span>
          <span className="h-2 overflow-hidden rounded-full bg-muted/60" aria-hidden="true">
            <span className="block h-full rounded-full bg-primary/70" style={{ width: `${max ? (row.count / max) * 100 : 0}%` }} />
          </span>
          <span className="text-right font-medium tabular-nums">{formatNumber(row.count)}</span>
        </li>
      ))}
    </ul>
  )
}
