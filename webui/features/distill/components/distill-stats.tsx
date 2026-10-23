import { ArchiveIcon, DatabaseIcon, GraduationCapIcon, ScaleIcon } from "lucide-react"
import type { ReactNode } from "react"

import { CopyButton } from "@/components/copy-button"
import { Hint } from "@/components/hint"
import { RelativeTime } from "@/components/relative-time"
import { ToneBadge } from "@/components/tone-badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import type { DistillStats } from "@/lib/api/types"
import { formatBytes, formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { judgeBucketLabel, NEGATIVE_OUTCOMES, outcomeLabel, POSITIVE_OUTCOMES } from "../lib/data"

function StatCard({ icon, label, supporting, hint, value, children }: { icon: ReactNode; label: string; supporting: ReactNode; hint: ReactNode; value: ReactNode; children?: ReactNode }) {
  return (
    <Card className="h-full">
      <CardHeader>
        <CardDescription className="flex items-center gap-2 [&_svg]:size-4">
          {icon}
          {label}
          <Hint label={label}>{hint}</Hint>
        </CardDescription>
        <CardTitle className="text-xl tabular-nums">{value}</CardTitle>
        <CardDescription className="truncate text-xs">{supporting}</CardDescription>
      </CardHeader>
      {children ? <CardContent className="flex min-w-0 flex-col gap-1.5">{children}</CardContent> : null}
    </Card>
  )
}

/** 一行「名称 · 占比条 · 数量」,分布小列表用。 */
function ShareRow({ label, count, total, title }: { label: ReactNode; count: number; total: number; title?: string }) {
  const pct = total > 0 ? (count / total) * 100 : 0
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_3rem_2.5rem] items-center gap-2 text-xs" title={title}>
      <span className="truncate text-muted-foreground">{label}</span>
      <span className="h-1.5 overflow-hidden rounded-full bg-muted/70" aria-hidden="true">
        <span className="block h-full rounded-full bg-primary/70" style={{ width: `${pct}%` }} />
      </span>
      <span className="text-right font-medium tabular-nums">{formatNumber(count)}</span>
    </div>
  )
}

/** 数据概况:判定记录的来源分布、真实结果、老师标注、存储与归档。没有数的分组不画占位。 */
export function DistillStatsCards({ stats }: { stats: DistillStats | undefined }) {
  if (!stats) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-4">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-52 w-full" />
        ))}
      </div>
    )
  }

  const sources = Object.entries(stats.by_source).sort((a, b) => b[1] - a[1])
  const judges = Object.entries(stats.by_judge).sort((a, b) => b[1] - a[1])
  const outcomes = Object.entries(stats.outcomes).sort((a, b) => b[1] - a[1])
  const outcomeTotal = outcomes.reduce((sum, [, n]) => sum + n, 0)
  const positive = outcomes.filter(([k]) => POSITIVE_OUTCOMES.has(k)).reduce((sum, [, n]) => sum + n, 0)
  const negative = outcomes.filter(([k]) => NEGATIVE_OUTCOMES.has(k)).reduce((sum, [, n]) => sum + n, 0)
  const teachers = Object.entries(stats.teacher_labels).sort((a, b) => b[1] - a[1])
  const labelTotal = teachers.reduce((sum, [, n]) => sum + n, 0)
  const coverage = stats.decisions > 0 ? Math.round((stats.decisions_with_direct_outcomes / stats.decisions) * 100) : null
  const archive = stats.archive
  const watermark = typeof archive.state?.decision_log === "string" ? (archive.state.decision_log as string) : null

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-4">
      <StatCard
        icon={<DatabaseIcon />}
        label="判定记录"
        supporting={stats.last_24h ? `近 24 小时 +${formatNumber(stats.last_24h)}` : "近 24 小时没有新增"}
        hint="每次 prime(以及 System 1 网关、评测与经验流)做出的注入决定各记一条:完整的判官题目、判官作答、候选信号与最终注入。"
        value={formatNumber(stats.decisions)}
      >
        {sources.map(([key, n]) => (
          <ShareRow key={key} label={labelOf.decisionSource(key).label} count={n} total={stats.decisions} title={`来源:${key}`} />
        ))}
        {judges.length > 0 ? (
          <p className="mt-1 truncate text-xs text-muted-foreground" title={judges.map(([k, n]) => `${judgeBucketLabel(k)} ${n}`).join("\n")}>
            判官:
            {judges.map(([k, n], i) => (
              <span key={k || "__none__"}>
                {i > 0 ? " · " : ""}
                {judgeBucketLabel(k)} <span className="font-medium text-foreground tabular-nums">{formatNumber(n)}</span>
              </span>
            ))}
          </p>
        ) : null}
      </StatCard>

      {outcomeTotal > 0 ? (
        <StatCard
          icon={<ScaleIcon />}
          label="真实结果"
          supporting={`${formatNumber(stats.decisions_with_direct_outcomes)} 条判定直接挂上了结果`}
          hint="事后回填的真实标签:report 的成功 / 失败、会话捕获的被动评估(有帮助、矛盾、本该想起)、评测的标准答案。进度条是直接挂上结果的判定占比;没带判定 id 的结果在导出时再按会话与情境找回。"
          value={formatNumber(outcomeTotal)}
        >
          <div className="flex flex-wrap gap-1">
            {outcomes.map(([key, n]) => {
              const label = outcomeLabel(key)
              return (
                <ToneBadge key={key} tone={label.tone} title={label.hint}>
                  {label.label} <span className="tabular-nums">{formatNumber(n)}</span>
                </ToneBadge>
              )
            })}
          </div>
          <p className="text-xs text-muted-foreground">
            正向 <span className="font-medium text-foreground tabular-nums">{formatNumber(positive)}</span> · 负向{" "}
            <span className="font-medium text-foreground tabular-nums">{formatNumber(negative)}</span>
          </p>
          {coverage !== null ? (
            <div className="flex items-center gap-2">
              <Progress value={coverage} aria-label="直接结果覆盖率" className={cn("h-1.5", coverage >= 20 ? "**:data-[slot=progress-indicator]:bg-emerald-500" : "**:data-[slot=progress-indicator]:bg-amber-500")} />
              <span className="w-9 shrink-0 text-right text-[10px] text-muted-foreground tabular-nums">{coverage}%</span>
            </div>
          ) : null}
        </StatCard>
      ) : null}

      {labelTotal > 0 || stats.shadow_judges.length > 0 ? (
        <StatCard
          icon={<GraduationCapIcon />}
          label="老师标注"
          supporting={stats.shadow_judges.length ? `影子判官:${stats.shadow_judges.join("、")}` : "没有配置影子判官"}
          hint="其他模型对同一道题的完整作答(软标签)。影子判官在每次判定后原样作答、不影响决定;也可以离线用 python -m app.distill.label 批量标注。每一行是该老师标过的判定占全部判定的比例。"
          value={formatNumber(labelTotal)}
        >
          {teachers.map(([name, n]) => (
            <ShareRow key={name} label={<span className="font-mono">{name}</span>} count={n} total={Math.max(stats.decisions, n)} />
          ))}
        </StatCard>
      ) : null}

      <StatCard
        icon={<ArchiveIcon />}
        label="存储与归档"
        supporting={archive.enabled ? `每 ${archive.hours} 小时归档 · 库内保留 ${archive.keep_days} 天` : "归档已关闭"}
        hint="判定数据定期追加导出成压缩 JSONL,长期保存供训练;数据库只清理已归档且超过保留期的记录。归档关闭时永不清理——训练数据宁可多占空间也不丢。"
        value={`${stats.storage_mb.toFixed(1)} MB`}
      >
        {archive.enabled ? (
          <>
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground">归档文件</span>
              <span className="font-medium tabular-nums">
                {formatNumber(archive.files ?? 0)} 个 · {formatBytes(archive.bytes ?? 0)}
              </span>
            </div>
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="text-muted-foreground">判定归档到</span>
              {watermark ? <RelativeTime value={watermark} className="font-medium" /> : <span className="text-muted-foreground">尚未归档</span>}
            </div>
            <div className="flex min-w-0 items-center gap-1 rounded-md border bg-muted/40 py-0.5 pr-0.5 pl-2">
              <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted-foreground" title={archive.dir}>
                {archive.dir}
              </span>
              <CopyButton value={archive.dir} label="归档目录" />
            </div>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">在运行参数里设置 distill.archive_dir 即可开启。</p>
        )}
      </StatCard>
    </div>
  )
}

export function DecisionLogBadge({ on }: { on: boolean }) {
  return on ? (
    <ToneBadge tone="success-light" title="运行参数 distill.decision_log 开启:每次判定都会写一条记录">
      <span className="size-1.5 rounded-full bg-emerald-500" aria-hidden="true" />
      记录中
    </ToneBadge>
  ) : (
    <ToneBadge tone="outline" title="运行参数 distill.decision_log 已关闭:新的判定不会被记录">
      <span className="size-1.5 rounded-full bg-muted-foreground" aria-hidden="true" />
      已停止记录
    </ToneBadge>
  )
}
