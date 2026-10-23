import { DatabaseIcon, GaugeIcon, MessageSquareReplyIcon, TargetIcon } from "lucide-react"
import type { ReactNode } from "react"

import { MetricCard } from "@/components/metric-card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import type { Dashboard } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { cn } from "@/lib/utils"

import { ratio } from "./data"

type Tone = "neutral" | "good" | "warn" | "bad"

const METER_TONE: Record<Tone, string> = {
  neutral: "",
  good: "**:data-[slot=progress-indicator]:bg-emerald-500",
  warn: "**:data-[slot=progress-indicator]:bg-amber-500",
  bad: "**:data-[slot=progress-indicator]:bg-destructive",
}

function Meter({ value, label, tone = "neutral" }: { value: number; label: string; tone?: Tone }) {
  return (
    <div className="flex items-center gap-2">
      <Progress value={value} aria-label={label} className={cn("h-1.5", METER_TONE[tone])} />
      <span className="w-9 shrink-0 text-right text-xs text-muted-foreground tabular-nums">{value}%</span>
    </div>
  )
}

interface Kpi {
  id: string
  label: string
  icon: ReactNode
  value: string
  detail: string
  /** 0~100;null 表示没有可算的样本,不画进度条。 */
  meter: number | null
  meterLabel: string
  tone: Tone
}

function buildKpis(data: Dashboard): Kpi[] {
  const status = data.by_status ?? {}
  const active = status.active ?? 0
  const quarantine = status.quarantine ?? 0
  const total = data.totals.enzymes
  const m = data.metrics
  const kpis: Kpi[] = []

  kpis.push({
    id: "size",
    label: "库规模",
    icon: <DatabaseIcon />,
    value: formatNumber(total),
    detail: `启用 ${formatNumber(active)}${quarantine ? ` · 待审核 ${formatNumber(quarantine)}` : ""}`,
    meter: total > 0 ? Math.round((active / total) * 100) : null,
    meterLabel: "启用占比",
    tone: "neutral",
  })
  if (total > 0) {
    kpis.push({
      id: "confidence",
      label: "平均置信度",
      icon: <GaugeIcon />,
      value: data.totals.avg_confidence.toFixed(2),
      detail: `累计推送 ${formatNumber(data.totals.activations)} 次 · 反馈 ${formatNumber(data.totals.triggers)} 次`,
      meter: Math.round(data.totals.avg_confidence * 100),
      meterLabel: "平均置信度",
      tone: "neutral",
    })
  }
  if (m.activated_enzymes > 0) {
    const coverage = m.feedback_coverage
    kpis.push({
      id: "coverage",
      label: "反馈覆盖率",
      icon: <MessageSquareReplyIcon />,
      value: ratio(coverage),
      detail: `已推送 ${formatNumber(m.activated_enzymes)} 条 · 有反馈 ${formatNumber(m.activated_and_reported)} 条`,
      meter: Math.round(coverage * 100),
      meterLabel: "反馈覆盖率",
      tone: coverage >= 0.6 ? "good" : coverage >= 0.3 ? "warn" : "bad",
    })
  }
  if (m.activation_weighted_precision !== null) {
    const precision = m.activation_weighted_precision
    kpis.push({
      id: "precision",
      label: "加权精度",
      icon: <TargetIcon />,
      value: ratio(precision),
      detail: m.false_positive_rate === null ? "暂无误报统计" : `误报率 ${ratio(m.false_positive_rate)}`,
      meter: Math.round(precision * 100),
      meterLabel: "加权精度",
      tone: precision >= 0.7 ? "good" : precision >= 0.4 ? "warn" : "bad",
    })
  }
  return kpis
}

export function KpiTiles({ data }: { data: Dashboard | undefined }) {
  if (!data) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-32" />
        ))}
      </div>
    )
  }
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {buildKpis(data).map((kpi) => (
        <MetricCard key={kpi.id} icon={kpi.icon} label={kpi.label} value={kpi.value} detail={kpi.detail}>
          {kpi.meter !== null ? <Meter value={kpi.meter} label={kpi.meterLabel} tone={kpi.tone} /> : null}
        </MetricCard>
      ))}
    </div>
  )
}
