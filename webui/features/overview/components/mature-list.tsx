import Link from "next/link"

import { Hint } from "@/components/hint"
import { ScoreBar } from "@/components/score-bar"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { Dashboard } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"

import { ratio } from "./data"

/** 最可靠的几条记忆:置信度与精度都高、被反复触发验证过。点一条打开记忆页里它的详情。没有成熟记忆时整张卡不出现。 */
export function MatureList({ data, className }: { data: Dashboard | undefined; className?: string }) {
  const rows = data?.mature ?? []
  if (data && rows.length === 0) return null
  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          成熟记忆
          <Hint label="成熟记忆">被反复触发、反馈证实有用、置信度与精度都高的记忆,是库里最可靠的部分;巩固时它们会被写进规则文件。</Hint>
        </CardTitle>
      </CardHeader>
      <CardContent className="p-1.5">
        {!data ? (
          <div className="flex flex-col gap-3 p-1.5">
            {Array.from({ length: 4 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : (
          <ul className="flex flex-col">
            {rows.map((row) => (
              <li key={row.id}>
                <Link href={`/enzymes?id=${encodeURIComponent(row.id)}`} className="flex items-center gap-3 rounded-lg px-2.5 py-2 transition-colors hover:bg-muted/50">
                  <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <p className="line-clamp-2 text-sm font-medium break-words">{row.then}</p>
                    <p className="truncate font-mono text-xs text-muted-foreground">{row.patterns}</p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <ScoreBar value={row.confidence} />
                    <span className="text-xs text-muted-foreground tabular-nums">
                      精度 {ratio(row.precision)} · 触发 {formatNumber(row.triggers)}
                    </span>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}
