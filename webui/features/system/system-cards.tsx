"use client"

import { ArrowRightIcon, BoxesIcon, CircleCheckIcon, CircleXIcon, CpuIcon, DatabaseIcon, InfoIcon, PlugZapIcon, type LucideIcon } from "lucide-react"
import Link from "next/link"
import type { ReactNode } from "react"

import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { SystemStatus } from "@/lib/api/types"
import { formatDateTime, formatDuration, formatNumber, formatRelative } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { CAPABILITIES, DEPENDENCIES } from "./lib/system"

export function HealthBadge({ ok, okLabel = "正常", badLabel = "异常" }: { ok: boolean; okLabel?: string; badLabel?: string }) {
  return (
    <ToneBadge tone={ok ? "success-light" : "destructive-light"} className="gap-1.5">
      <span className={cn("size-1.5 shrink-0 rounded-full", ok ? "bg-emerald-500" : "bg-destructive")} aria-hidden="true" />
      {ok ? okLabel : badLabel}
    </ToneBadge>
  )
}

const DEPENDENCY_ICON: Record<string, LucideIcon> = {
  postgresql: DatabaseIcon,
  qdrant: BoxesIcon,
  mcp: PlugZapIcon,
  workers: CpuIcon,
}

function dependencyDetail(key: string, system: SystemStatus): string {
  switch (key) {
    case "postgresql":
      return system.pg_version ? `PostgreSQL ${system.pg_version} · 结构版本 v${system.schema_version ?? "—"}` : (system.postgresql_error ?? "连接失败")
    case "qdrant":
      return system.qdrant ? `集合 ${system.qdrant.collection} · ${formatNumber(system.qdrant.documents)} 条向量` : (system.qdrant_error ?? "连接失败")
    case "mcp":
      return system.checks.mcp ? "工具端点已启动" : "工具端点没有启动,智能体连不上 MCP"
    case "workers": {
      const running = Object.values(system.jobs ?? {}).reduce((sum, counts) => sum + (counts.running ?? 0), 0)
      const pending = Object.values(system.jobs ?? {}).reduce((sum, counts) => sum + (counts.pending ?? 0), 0)
      return system.checks.workers ? `运行中 ${running} · 排队 ${pending}` : "后台循环已退出,巩固与捕获不会执行"
    }
  }
  return ""
}

/** 四项依赖并排:图标 + 名称 + 彩色状态点 + 连通详情(版本、向量数、排队数,或者连不上的原因)。 */
export function DependencyCards({ system, loading }: { system: SystemStatus | undefined; loading: boolean }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {DEPENDENCIES.map((dependency) => {
        const Icon = DEPENDENCY_ICON[dependency.key]
        const ok = Boolean(system?.checks[dependency.key])
        const detail = system ? dependencyDetail(dependency.key, system) : ""
        return (
          <Card key={dependency.key} className="py-4">
            <CardContent className="flex min-w-0 items-center gap-3">
              <span
                className={cn(
                  "flex size-10 shrink-0 items-center justify-center rounded-xl border bg-muted [&_svg]:size-5",
                  system && !ok ? "[&_svg]:text-destructive" : "[&_svg]:text-foreground",
                )}
                aria-hidden="true"
              >
                <Icon />
              </span>
              <div className="flex min-w-0 flex-col gap-1.5">
                <span className="text-sm leading-none font-medium" title={dependency.role}>
                  {dependency.label}
                </span>
                {loading || !system ? (
                  <Skeleton className="h-3 w-32" />
                ) : (
                  <div className="flex min-w-0 items-center gap-2" title={detail}>
                    <span className={cn("flex shrink-0 items-center gap-1 text-xs leading-none", ok ? "text-emerald-600" : "text-destructive")}>
                      <span className={cn("size-1.5 rounded-full", ok ? "bg-emerald-600" : "bg-destructive")} aria-hidden="true" />
                      {ok ? "正常" : "异常"}
                    </span>
                    <p className={cn("truncate text-xs leading-none", ok ? "text-muted-foreground" : "text-destructive")}>{detail}</p>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

/** 一行「名字 — 值」的只读行。 */
function Fact({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-4 px-6 py-1.5">
      <span className="flex min-w-0 items-center gap-1 text-sm text-muted-foreground">
        <span className="truncate">{label}</span>
        {hint ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button type="button" variant="ghost" size="icon-xs" className="-my-1 shrink-0 text-muted-foreground hover:text-foreground" aria-label={`${label}说明`}>
                <InfoIcon aria-hidden="true" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-64 text-xs leading-relaxed">
              {hint}
            </TooltipContent>
          </Tooltip>
        ) : null}
      </span>
      <span className="min-w-0 truncate text-right text-sm font-medium tabular-nums">{children}</span>
    </div>
  )
}

function FactCard({
  title,
  description,
  link,
  children,
}: {
  title: string
  description?: ReactNode
  link?: { href: string; label: string }
  children: ReactNode
}) {
  return (
    <Card className="h-full gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle className="text-sm">{title}</CardTitle>
        {description ? <CardDescription className="truncate text-xs">{description}</CardDescription> : null}
      </CardHeader>
      <CardContent className="flex flex-col divide-y px-0">{children}</CardContent>
      {link ? (
        <CardFooter className="justify-end border-t py-2">
          <Button size="xs" variant="ghost" asChild>
            <Link href={link.href}>
              {link.label} <ArrowRightIcon aria-hidden="true" />
            </Link>
          </Button>
        </CardFooter>
      ) : null}
    </Card>
  )
}

function YesNo({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", !ok && "text-muted-foreground")}>
      {ok ? <CircleCheckIcon className="size-4 text-emerald-600" aria-hidden="true" /> : <CircleXIcon className="size-4 text-muted-foreground" aria-hidden="true" />}
      {ok ? yes : no}
    </span>
  )
}

export function RuntimeCard({ system }: { system: SystemStatus }) {
  return (
    <FactCard title="运行环境" description={`已连续运行 ${formatDuration(system.uptime_s)}`}>
      <Fact label="版本">
        <span className="font-mono">{system.version}</span>
      </Fact>
      <Fact label="Python">
        <span className="font-mono">{system.python}</span>
      </Fact>
      <Fact label="平台">
        <span className="font-mono" title={system.platform}>
          {system.platform}
        </span>
      </Fact>
      <Fact label="启动于">
        {formatDateTime(system.started_at)} · <span className="font-normal">{formatRelative(system.started_at)}</span>
      </Fact>
    </FactCard>
  )
}

export function VectorCard({ system }: { system: SystemStatus }) {
  const pending = system.vector_sync_pending ?? 0
  return (
    <FactCard title="向量索引" description={system.qdrant ? `${formatNumber(system.qdrant.documents)} 条向量` : "Qdrant 不可用"}>
      <Fact label="地址">
        <span className="font-mono">{system.qdrant?.url ?? "—"}</span>
      </Fact>
      <Fact label="集合">
        <span className="font-mono">{system.qdrant?.collection ?? "—"}</span>
      </Fact>
      <Fact label="向量条数">{system.qdrant ? formatNumber(system.qdrant.documents) : "—"}</Fact>
      <Fact label="等待同步" hint="已写入主库、还没写进向量索引的记忆。长时间不降,通常是向量模型没配好或调用失败。">
        <span className={cn(pending > 0 && "text-amber-700 dark:text-amber-400")}>{formatNumber(pending)}</span>
      </Fact>
    </FactCard>
  )
}

export function JobsCard({ system }: { system: SystemStatus }) {
  const kinds = Object.entries(system.jobs ?? {})
  return (
    <FactCard title="后台任务" description={system.checks.workers ? "执行器运行中" : "执行器已停止"} link={{ href: "/admin/jobs", label: "查看任务队列" }}>
      {kinds.length ? (
        kinds.map(([kind, counts]) => {
          const label = labelOf.jobKind(kind)
          return (
            <div key={kind} className="flex min-h-9 flex-wrap items-center justify-between gap-2 px-6 py-2">
              <span className="text-sm text-muted-foreground">{label.label}</span>
              <span className="flex flex-wrap justify-end gap-1">
                {Object.entries(counts)
                  .filter(([, count]) => count > 0)
                  .map(([status, count]) => {
                    const statusLabel = labelOf.jobStatus(status)
                    return (
                      <ToneBadge key={status} tone={statusLabel.tone} className="tabular-nums">
                        {statusLabel.label} {formatNumber(count)}
                      </ToneBadge>
                    )
                  })}
              </span>
            </div>
          )
        })
      ) : (
        <p className="px-6 py-3 text-sm text-muted-foreground">还没有执行过后台任务。</p>
      )}
    </FactCard>
  )
}

export function CapabilitiesCard({ system }: { system: SystemStatus }) {
  const ready = CAPABILITIES.filter((item) => system.capabilities[item.key]).length
  return (
    <FactCard title="模型能力" description={`${ready} / ${CAPABILITIES.length} 项可用`} link={{ href: "/models/bindings", label: "模型绑定" }}>
      {CAPABILITIES.map((item) => (
        <Fact key={item.key} label={item.label} hint={item.hint}>
          <YesNo ok={system.capabilities[item.key]} yes="可用" no="未配置" />
        </Fact>
      ))}
    </FactCard>
  )
}

export function AuthCard({ system }: { system: SystemStatus }) {
  return (
    <FactCard
      title="访问认证"
      description={system.auth.enabled ? "需要登录才能访问" : "未开启:任何人都能访问"}
      link={{ href: "/settings/security", label: "访问与安全" }}
    >
      <Fact label="登录认证">
        <YesNo ok={system.auth.enabled} yes="已开启" no="已关闭" />
      </Fact>
      <Fact label="API 令牌" hint="钩子、园丁等机器调用 REST API 用的静态令牌">
        <YesNo ok={system.auth.api_token_set} yes="已设置" no="未设置" />
      </Fact>
      <Fact label="MCP 令牌" hint="智能体连接 MCP 服务时出示的令牌">
        <YesNo ok={system.auth.mcp_token_set} yes="已设置" no="未设置" />
      </Fact>
    </FactCard>
  )
}

const SOURCE_ORDER = ["db", "env", "default"] as const
const SOURCE_COLOR: Record<(typeof SOURCE_ORDER)[number], string> = {
  db: "var(--chart-1)",
  env: "var(--chart-2)",
  default: "var(--chart-3)",
}

export function SourcesCard({ system }: { system: SystemStatus }) {
  const total = SOURCE_ORDER.reduce((sum, key) => sum + (system.settings[key] ?? 0), 0)
  return (
    <FactCard
      title="配置来源"
      description={`${formatNumber(total)} 项运行参数 · ${system.registry.providers} 个服务商 · ${system.registry.models} 个模型`}
      link={{ href: "/settings/runtime", label: "运行参数" }}
    >
      <div className="flex flex-col gap-3 px-6 py-3">
        <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted" role="img" aria-label="配置来源占比">
          {SOURCE_ORDER.map((key) =>
            system.settings[key] ? (
              <div key={key} className="h-full first:rounded-s-full last:rounded-e-full" style={{ width: `${(system.settings[key] / Math.max(1, total)) * 100}%`, background: SOURCE_COLOR[key] }} />
            ) : null,
          )}
        </div>
        <ul className="flex flex-col gap-1.5">
          {SOURCE_ORDER.filter((key) => (system.settings[key] ?? 0) > 0).map((key) => {
            const label = labelOf.source(key)
            return (
              <li key={key} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex items-center gap-2">
                  <span className="size-2 shrink-0 rounded-full" style={{ background: SOURCE_COLOR[key] }} aria-hidden="true" />
                  <span className="text-muted-foreground" title={label.hint}>
                    {label.label}
                  </span>
                </span>
                <span className="font-medium tabular-nums">{formatNumber(system.settings[key] ?? 0)}</span>
              </li>
            )
          })}
        </ul>
      </div>
    </FactCard>
  )
}
