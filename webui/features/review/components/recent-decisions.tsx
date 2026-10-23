"use client"

import { useQuery } from "@tanstack/react-query"

import { RelativeTime } from "@/components/relative-time"
import { ErrorState } from "@/components/states"
import { ToneBadge, type Tone } from "@/components/tone-badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { AuditEntry } from "@/lib/api/types"
import { shortId } from "@/lib/format"

const AUDIT_PARAMS = { action: "enzyme", limit: 60 }
const SHOWN = 6

const OUTCOME: Record<string, { label: string; tone: Tone }> = {
  "enzyme.bulk_activate": { label: "通过", tone: "success-light" },
  "enzyme.bulk_reject": { label: "拒绝", tone: "outline" },
  "enzyme.bulk_delete": { label: "删除", tone: "destructive-light" },
  "enzyme.bulk_quarantine": { label: "送审", tone: "warning-light" },
}

function idsOf(entry: AuditEntry): string[] {
  const ids = (entry.detail as { ids?: unknown }).ids
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : []
}

/** 审计日志里最近的批量通过 / 拒绝 / 删除 / 送审,所以只有管理员看得到。审计只记 id,单条的可以点开。 */
export function RecentDecisions({ onOpenEnzyme, className }: { onOpenEnzyme: (id: string) => void; className?: string }) {
  const audit = useQuery({
    queryKey: qk.audit(AUDIT_PARAMS),
    queryFn: () => adminApi.audit(AUDIT_PARAMS),
    select: (data) => data.items.filter((entry) => entry.action in OUTCOME && idsOf(entry).length > 0).slice(0, SHOWN),
  })
  const decisions = audit.data ?? []

  // 没有记录就不占位置
  if (audit.isSuccess && decisions.length === 0) return null

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>最近处理</CardTitle>
      </CardHeader>
      <CardContent>
        {audit.isPending ? (
          <div className="flex flex-col gap-3">
            {Array.from({ length: 4 }).map((_, index) => (
              <Skeleton key={index} className="h-8 w-full" />
            ))}
          </div>
        ) : audit.isError ? (
          <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
        ) : (
          <ul className="flex flex-col divide-y">
            {decisions.map((entry) => {
              const outcome = OUTCOME[entry.action]
              const ids = idsOf(entry)
              const openable = ids.length === 1 && entry.action !== "enzyme.bulk_delete"
              return (
                <li key={entry.id} className="flex items-center justify-between gap-3 py-2 first:pt-0 last:pb-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <ToneBadge tone={outcome.tone} label={outcome.label} />
                    {ids.length > 1 ? (
                      <span className="text-sm font-medium">{ids.length} 条记忆</span>
                    ) : openable ? (
                      <button type="button" className="font-mono text-sm underline-offset-2 hover:underline" title={`打开 ${ids[0]}`} onClick={() => onOpenEnzyme(ids[0])}>
                        {shortId(ids[0])}
                      </button>
                    ) : (
                      <span className="font-mono text-sm" title={ids[0]}>
                        {shortId(ids[0])}
                      </span>
                    )}
                  </div>
                  <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                    <span className="font-medium text-foreground">{entry.actor}</span>
                    <RelativeTime value={entry.created_at} />
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
