import { MetricCard } from "@/components/metric-card"
import { Skeleton } from "@/components/ui/skeleton"
import type { EnzymeRow } from "@/lib/api/types"
import { formatNumber, formatPercent } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { QUALITY_GATE } from "@/features/enzymes/lib/options"

import { waitingDays, waitingFor } from "../lib/review-data"

/** 四格:待审核多少、最久等了多久、主要来自哪、质量分如何。队列为空时整块不出现。 */
export function ReviewSummary({
  items,
  queueTotal,
  libraryTotal,
  loading,
  now,
}: {
  items: EnzymeRow[]
  queueTotal: number
  libraryTotal: number
  loading: boolean
  /** 由页面统一给出的「现在」,几格的时长按同一个时刻算 */
  now: number
}) {
  if (loading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-32" />
        ))}
      </div>
    )
  }
  if (!items.length) return null

  const fresh = items.filter((item) => waitingDays(item.created_at, now) < 1).length
  const oldest = items.reduce<EnzymeRow | null>((current, item) => (!current || (item.created_at ?? "") < (current.created_at ?? "") ? item : current), null)
  const oldestDays = oldest ? waitingDays(oldest.created_at, now) : 0

  const byOrigin = new Map<string, number>()
  for (const item of items) byOrigin.set(item.origin, (byOrigin.get(item.origin) ?? 0) + 1)
  const origins = [...byOrigin.entries()].sort((a, b) => b[1] - a[1])
  const [topOrigin, topCount] = origins[0]

  const assessed = items.filter((item) => item.quality_score > 0)
  const average = assessed.length ? assessed.reduce((total, item) => total + item.quality_score, 0) / assessed.length : null
  const low = assessed.filter((item) => item.quality_score < QUALITY_GATE.refine).length

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <MetricCard
        label="待审核"
        value={formatNumber(queueTotal)}
        detail={[
          fresh ? `24 小时内新增 ${formatNumber(fresh)}` : "",
          libraryTotal ? `占全部记忆 ${formatPercent(queueTotal / libraryTotal)}` : "",
        ]
          .filter(Boolean)
          .join(" · ") || "质量门拦下,暂不参与召回"}
      />
      <MetricCard
        label="最久等待"
        value={oldest ? waitingFor(oldest.created_at, now) : "—"}
        warn={oldestDays >= 7}
        detail={<span className="line-clamp-1" title={oldest?.then}>{oldestDays >= 7 ? "超过 7 天 · " : ""}{oldest?.then}</span>}
      />
      <MetricCard
        label="主要来源"
        value={labelOf.origin(topOrigin).label}
        detail={
          origins.length > 1
            ? [`${formatNumber(topCount)} 条`, ...origins.slice(1, 4).map(([origin, count]) => `${labelOf.origin(origin).label} ${count}`)].join(" · ")
            : `${formatNumber(topCount)} 条,队列里全部来自这一种来源`
        }
      />
      {average !== null ? (
        <MetricCard
          label="平均质量分"
          value={average.toFixed(1)}
          warn={low > 0}
          detail={`${formatNumber(assessed.length)} 条经过评分 · 自动通过线 ${QUALITY_GATE.pass}${low ? ` · ${formatNumber(low)} 条低于 ${QUALITY_GATE.refine}` : ""}`}
        />
      ) : null}
    </div>
  )
}
