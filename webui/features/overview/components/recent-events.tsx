import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"

import { Hint } from "@/components/hint"
import { RelativeTime } from "@/components/relative-time"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { Dashboard } from "@/lib/api/types"
import { shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { describeEvent } from "./data"

/** 卡片里最多列几条;更多的在「进化事件」页。 */
const MAX_EVENTS = 6

/** 记忆库最近发生的改动。没有进化事件时整张卡不出现。 */
export function RecentEvents({ data, className }: { data: Dashboard | undefined; className?: string }) {
  const events = (data?.recent_events ?? []).slice(0, MAX_EVENTS)
  if (data && events.length === 0) return null
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          最新进化
          <Hint label="最新进化">记忆库最近发生的改动:反馈学习、巩固合并、经验样本、免疫修剪、情景回放……每一条都可以追溯到具体的记忆。</Hint>
        </CardTitle>
        <CardAction>
          <Button variant="ghost" size="xs" asChild>
            <Link href="/learning/evolution">
              全部事件
              <ArrowRightIcon />
            </Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent>
        {!data ? (
          <div className="flex flex-col gap-4">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : (
          <ul className="flex flex-col divide-y">
            {events.map((event, index) => {
              const label = labelOf.event(event.event_type)
              const summary = describeEvent(event)
              return (
                <li key={event.id ?? `${event.event_type}-${index}`} className="flex flex-col gap-1 py-2.5 first:pt-0 last:pb-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <ToneBadge tone={label.tone} label={label.label} />
                    <RelativeTime value={event.created_at} className="text-xs text-muted-foreground" />
                  </div>
                  {summary ? <p className="line-clamp-2 text-xs leading-relaxed break-words">{summary}</p> : null}
                  {event.enzyme_id ? (
                    <Link href={`/enzymes?id=${encodeURIComponent(event.enzyme_id)}`} className="w-fit font-mono text-xs text-muted-foreground transition-colors hover:text-foreground">
                      {shortId(event.enzyme_id)}
                    </Link>
                  ) : null}
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
