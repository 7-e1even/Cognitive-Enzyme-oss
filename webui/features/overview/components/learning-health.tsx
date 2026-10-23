import { ArrowRightIcon, CheckIcon, XIcon } from "lucide-react"
import Link from "next/link"
import type { ReactNode } from "react"

import { Hint } from "@/components/hint"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { Dashboard } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { CAPABILITIES } from "./data"
import { SegmentedBar } from "./bars"

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-xs">
      <span className="text-muted-foreground" title={hint}>
        {label}
      </span>
      <span className="font-medium tabular-nums">{children}</span>
    </div>
  )
}

function JobLine({ kind, counts }: { kind: string; counts: Record<string, number> }) {
  const queued = (counts.pending ?? 0) + (counts.running ?? 0)
  const failed = counts.failed ?? 0
  return (
    <div className="flex items-center justify-between gap-3 text-xs">
      <span className="text-muted-foreground">{labelOf.jobKind(kind).label}</span>
      <span className="flex items-center gap-2 tabular-nums">
        <span>成功 {formatNumber(counts.succeeded ?? 0)}</span>
        {failed > 0 ? <span className="text-destructive">失败 {formatNumber(failed)}</span> : null}
        {queued > 0 ? <span className="text-sky-600 dark:text-sky-400">进行中 {formatNumber(queued)}</span> : null}
      </span>
    </div>
  )
}

/** 学习环有没有在转:反馈把记忆往哪个方向推、回放考试的结论、后台任务是否在跑、模型能力是否齐全。每一项都是后端原值。 */
export function LearningHealth({ data, isAdmin, className }: { data: Dashboard | undefined; isAdmin: boolean; className?: string }) {
  const m = data?.metrics
  const jobs = data?.jobs ?? {}
  const jobKinds = Object.keys(jobs).sort()

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          学习健康度
          <Hint label="学习健康度">记忆库是不是真的在从使用中学习:反馈把置信度往哪个方向推、情景回放考试的结论、后台任务是否正常、学习所需的模型是否配置齐全。</Hint>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!data || !m ? (
          <div className="flex flex-col gap-4">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-20 w-full" />
          </div>
        ) : (
          <>
            <SegmentedBar
              title="反馈学习 · 近 30 天"
              unit="次"
              segments={[
                { key: "reinforcing", label: "强化", count: m.hebbian_30d.reinforcing, color: "var(--color-emerald-500)", hint: "反馈证实有用,置信度上调" },
                { key: "corrective", label: "纠正", count: m.hebbian_30d.corrective, color: "var(--color-amber-500)", hint: "反馈判为误报,置信度下调" },
              ]}
              action={
                m.hebbian_30d.negations_learned ? (
                  <span className="text-xs text-muted-foreground" title="从误报里学到的「这种情况下不适用」">
                    否定条件 <span className="font-medium text-foreground tabular-nums">{m.hebbian_30d.negations_learned}</span>
                  </span>
                ) : null
              }
            />
            <SegmentedBar
              title="情景回放 · 近 30 天"
              unit="次"
              segments={[
                { key: "helped", label: "有帮助", count: m.replay_30d.helped, color: "var(--color-emerald-500)" },
                { key: "contradicted", label: "矛盾", count: m.replay_30d.contradicted, color: "var(--color-rose-500)" },
                { key: "neutral", label: "无影响", count: Math.max(0, m.replay_30d.total - m.replay_30d.helped - m.replay_30d.contradicted), color: "var(--color-slate-400)" },
              ]}
              action={
                <span className="text-xs text-muted-foreground">
                  共 <span className="font-medium text-foreground tabular-nums">{formatNumber(m.replay_30d.total)}</span> 次
                </span>
              }
            />
            {m.activated_enzymes > 0 || m.speculative_active > 0 ? (
              <div className="flex flex-col gap-2">
                {m.activated_enzymes > 0 ? (
                  <Row label="已推送的记忆" hint="至少被 prime 注入过一次的记忆条数">
                    {formatNumber(m.activated_enzymes)} 条 · 有反馈 {formatNumber(m.activated_and_reported)}
                  </Row>
                ) : null}
                {m.speculative_active > 0 ? (
                  <Row label="试探性记忆" hint="由突变 / 梦境演化产生、从未被触发且置信度低于 0.35 的启用记忆;太多会稀释召回">
                    {formatNumber(m.speculative_active)} 条
                  </Row>
                ) : null}
              </div>
            ) : null}
            {jobKinds.length ? (
              <>
                <Separator />
                <div className="flex flex-col gap-2">
                  <div className="flex items-center justify-between gap-2">
                    <h4 className="text-xs font-medium text-muted-foreground">后台任务</h4>
                    {isAdmin ? (
                      <Button variant="ghost" size="xs" asChild>
                        <Link href="/admin/jobs">
                          查看
                          <ArrowRightIcon />
                        </Link>
                      </Button>
                    ) : null}
                  </div>
                  {jobKinds.map((kind) => (
                    <JobLine key={kind} kind={kind} counts={jobs[kind]} />
                  ))}
                </div>
              </>
            ) : null}
            <Separator />
            <div className="flex flex-col gap-2">
              <h4 className="text-xs font-medium text-muted-foreground">模型能力</h4>
              <div className="grid grid-cols-4 gap-1.5">
                {CAPABILITIES.map((cap) => {
                  const ok = data.capabilities[cap.key]
                  return (
                    <Tooltip key={cap.key}>
                      <TooltipTrigger asChild>
                        <span
                          className={cn(
                            "flex items-center justify-center gap-1 rounded-md border px-1.5 py-1 text-xs font-medium",
                            ok ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400" : "border-border bg-muted/40 text-muted-foreground",
                          )}
                        >
                          {ok ? <CheckIcon className="size-3" aria-hidden="true" /> : <XIcon className="size-3" aria-hidden="true" />}
                          {cap.label}
                          <span className="sr-only">{ok ? "已就绪" : "未配置"}</span>
                        </span>
                      </TooltipTrigger>
                      <TooltipContent className="max-w-60">{ok ? `${cap.label} 已就绪` : `${cap.label} 未配置:${cap.missing}`}</TooltipContent>
                    </Tooltip>
                  )
                })}
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
