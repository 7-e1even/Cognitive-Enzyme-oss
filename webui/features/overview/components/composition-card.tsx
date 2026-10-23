import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"

import { Hint } from "@/components/hint"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import type { Dashboard } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"

import { BarList, SegmentedBar, type Segment } from "./bars"
import { domainLabel, sortedEntries } from "./data"

const STATUS_COLOR: Record<string, string> = {
  active: "var(--color-emerald-500)",
  quarantine: "var(--color-amber-500)",
  rejected: "var(--color-slate-400)",
}
const SEVERITY_COLOR: Record<string, string> = {
  critical: "var(--color-rose-500)",
  warning: "var(--color-amber-500)",
  info: "var(--color-sky-500)",
}

function segmentsOf(record: Record<string, number>, order: string[], colors: Record<string, string>, label: (key: string) => { label: string; hint?: string }): Segment[] {
  const keys = [...order, ...Object.keys(record).filter((key) => !order.includes(key))]
  return keys
    .filter((key) => (record[key] ?? 0) > 0)
    .map((key) => ({ key, label: label(key).label, hint: label(key).hint, count: record[key], color: colors[key] ?? "var(--color-slate-400)" }))
}

/** 记忆构成:状态与严重度是「一个整体分成几份」(分段条),来源与领域是「谁多谁少」(横条)。 */
export function CompositionCard({ data, className }: { data: Dashboard | undefined; className?: string }) {
  const status = data ? segmentsOf(data.by_status, ["active", "quarantine", "rejected"], STATUS_COLOR, labelOf.status) : []
  const severity = data ? segmentsOf(data.by_severity, ["critical", "warning", "info"], SEVERITY_COLOR, labelOf.severity) : []
  const origins = sortedEntries(data?.by_origin).map(([key, count]) => ({ key, label: labelOf.origin(key).label, count }))
  const domains = sortedEntries(data?.top_domains).map(([key, count]) => ({ key: key || "__none__", label: domainLabel(key), count }))
  const quarantine = data?.by_status.quarantine ?? 0

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5">
          记忆构成
          <Hint label="记忆构成">记忆按审核状态、严重度、来源与领域的分布。来源说明记忆是怎么来的:手动创建、从会话沉淀、梦境演化或巩固合并;领域只列最多的 8 个。</Hint>
        </CardTitle>
        <CardAction>
          <Button variant="ghost" size="xs" asChild>
            <Link href="/enzymes">
              记忆
              <ArrowRightIcon />
            </Link>
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {!data ? (
          <div className="flex flex-col gap-5">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : (
          <>
            <SegmentedBar
              title="审核状态"
              segments={status}
              action={
                quarantine > 0 ? (
                  <Link href="/review" className="inline-flex items-center gap-0.5 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
                    {formatNumber(quarantine)} 条待审核
                    <ArrowRightIcon className="size-3" aria-hidden="true" />
                  </Link>
                ) : null
              }
            />
            <SegmentedBar title="严重度" segments={severity} />
            {origins.length || domains.length ? (
              <Tabs defaultValue={origins.length ? "origin" : "domain"} className="gap-3">
                <TabsList variant="line" className="h-8 gap-4 p-0">
                  {origins.length ? (
                    <TabsTrigger value="origin" className="px-0 text-xs">
                      来源
                    </TabsTrigger>
                  ) : null}
                  {domains.length ? (
                    <TabsTrigger value="domain" className="px-0 text-xs">
                      领域
                    </TabsTrigger>
                  ) : null}
                </TabsList>
                <TabsContent value="origin">
                  <BarList rows={origins} />
                </TabsContent>
                <TabsContent value="domain">
                  <BarList rows={domains} />
                </TabsContent>
              </Tabs>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  )
}
