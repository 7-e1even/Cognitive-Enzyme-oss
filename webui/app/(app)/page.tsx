"use client"

import { useQuery } from "@tanstack/react-query"
import { Activity, Database, ThumbsUp, Zap } from "lucide-react"
import Link from "next/link"
import type { ReactNode } from "react"
import { Area, AreaChart, CartesianGrid, XAxis } from "recharts"

import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart"
import { Skeleton } from "@/components/ui/skeleton"
import { dashboardApi } from "@/lib/api/endpoints"
import { useHealth } from "@/lib/api/hooks"
import type { HomeSummary } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { relativeTime } from "@/lib/relative-time"

const chartConfig = {
  injected: { label: "注入", color: "var(--chart-1)" },
  helped: { label: "有用", color: "var(--chart-2)" },
} satisfies ChartConfig

function Stat({
  icon, label, value, detail, href, warn,
}: { icon: ReactNode; label: string; value: string; detail: string; href?: string; warn?: boolean }) {
  const body = (
    <Card className={href ? "transition-colors hover:bg-muted/40" : undefined}>
      <CardHeader>
        <CardDescription className="flex items-center gap-2">
          {icon}
          {label}
        </CardDescription>
        <CardTitle className="text-xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      <CardContent className={warn ? "text-sm text-destructive" : "text-sm text-muted-foreground"}>{detail}</CardContent>
    </Card>
  )
  return href ? <Link href={href}>{body}</Link> : body
}

function cards(home: HomeSummary, memories: number | undefined, healthy: boolean | undefined) {
  const { days, primes, injected, helped, harmed } = home.impact
  const impact = !home.decision_log
    ? { value: "没有统计", detail: "判定记录已关闭,注入和反馈都没有记下来" }
    : helped === 0 && harmed === 0
      ? { value: "还没有反馈", detail: injected ? `近 ${days} 天注入 ${formatNumber(injected)} 条,还不知道有没有用` : "有注入之后开始统计" }
      : { value: `${formatNumber(helped)} 次有用`, detail: harmed ? `近 ${days} 天 · ${formatNumber(harmed)} 次说反了` : `近 ${days} 天 · 没有说反过` }
  return { impact, days, primes, injected, memories, healthy }
}

export default function HomePage() {
  const home = useQuery({ queryKey: ["home"], queryFn: () => dashboardApi.home(Intl.DateTimeFormat().resolvedOptions().timeZone) })
  const dash = useQuery({ queryKey: ["dashboard"], queryFn: () => dashboardApi.get(14) })
  const health = useHealth()

  if (home.isPending) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-32" />
        ))}
      </div>
    )
  }
  if (home.isError) return <p className="text-sm text-destructive">读取首页数据失败。</p>

  const h = home.data
  const c = cards(h, dash.data?.totals.enzymes, health.data ? health.data.status === "ok" : undefined)
  const storageOk = h.storage.postgresql && h.storage.qdrant

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          icon={<Database className="size-4" />}
          label="记忆库"
          value={c.memories === undefined ? "—" : `${formatNumber(c.memories)} 条记忆`}
          detail={h.pending_review ? `${formatNumber(h.pending_review)} 条待审核` : "没有待审核"}
          href="/enzymes"
        />
        <Stat
          icon={<Zap className="size-4" />}
          label="召回"
          value={`注入 ${formatNumber(c.injected)} 条`}
          detail={h.last_prime_at ? `近 ${c.days} 天 ${formatNumber(c.primes)} 次提问 · 最近一次 ${relativeTime(h.last_prime_at)}` : "还没有智能体接入"}
          href={h.last_prime_at ? undefined : "/settings/integrations"}
        />
        <Stat icon={<ThumbsUp className="size-4" />} label="帮上忙了吗" value={c.impact.value} detail={c.impact.detail} />
        <Stat
          icon={<Activity className="size-4" />}
          label="服务状态"
          value={storageOk ? "正常" : "有服务异常"}
          detail={storageOk ? "PostgreSQL · Qdrant 在线" : "检查 Qdrant 是否在运行"}
          warn={!storageOk}
          href="/admin/system"
        />
      </div>

      {h.series.some((p) => p.injected > 0) ? (
        <Card>
          <CardHeader>
            <CardTitle>近 {c.days} 天注入与有用</CardTitle>
            <CardDescription>按你所在时区切日</CardDescription>
          </CardHeader>
          <CardContent>
            <ChartContainer config={chartConfig} className="h-56 w-full">
              <AreaChart data={h.series}>
                <CartesianGrid vertical={false} />
                <XAxis dataKey="day" tickLine={false} axisLine={false} tickFormatter={(v: string) => v.slice(5)} />
                <ChartTooltip content={<ChartTooltipContent />} />
                <Area dataKey="injected" type="monotone" fill="var(--color-injected)" fillOpacity={0.2} stroke="var(--color-injected)" />
                <Area dataKey="helped" type="monotone" fill="var(--color-helped)" fillOpacity={0.2} stroke="var(--color-helped)" />
              </AreaChart>
            </ChartContainer>
          </CardContent>
        </Card>
      ) : null}

      {!h.capabilities.llm || !h.capabilities.embedding ? (
        <Card>
          <CardHeader>
            <CardTitle>还差两步</CardTitle>
            <CardDescription>必需的模型没配好,记忆还不能工作。</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2">
            {!h.capabilities.llm ? <Badge variant="outline">对话模型</Badge> : null}
            {!h.capabilities.embedding ? <Badge variant="outline">向量模型</Badge> : null}
            <Link href="/models/bindings" className="text-sm underline underline-offset-4">去绑定</Link>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
