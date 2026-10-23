"use client"

import { ArrowDownIcon, ArrowUpIcon, CornerDownRightIcon, FlagIcon, Trash2Icon, TriangleAlertIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import type { ModelEntry, Provider, RoleStatus } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import { KindBadge } from "./bits"
import { JUDGE_ALIAS, unusableReason } from "./lib/registry"

/**
 * 判官路由:从上到下依次询问,用上移 / 下移调整顺序。每一步是一位判官,步与步之间写明「没把握才升级」,
 * 最后一位写明「无论把握如何都返回」——顺序就是这条链的全部语义,所以它必须一眼读得出来。
 */
export function RouteChain({
  route,
  onChange,
  models,
  providers,
  judgeRole,
}: {
  route: string[]
  onChange: (route: string[]) => void
  models: ModelEntry[]
  providers: Provider[]
  /** 「jev」别名解析到的角色(model.judge) */
  judgeRole?: RoleStatus
}) {
  function move(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= route.length) return
    const next = [...route]
    ;[next[index], next[target]] = [next[target], next[index]]
    onChange(next)
  }
  return (
    <ol className="flex flex-col" aria-label="判官路由顺序">
      {route.map((id, index) => (
        <RouteStep
          key={id}
          id={id}
          index={index}
          last={index === route.length - 1}
          models={models}
          providers={providers}
          judgeRole={judgeRole}
          onMove={(delta) => move(index, delta)}
          onRemove={() => onChange(route.filter((item) => item !== id))}
        />
      ))}
    </ol>
  )
}

function RouteStep({
  id,
  index,
  last,
  models,
  providers,
  judgeRole,
  onMove,
  onRemove,
}: {
  id: string
  index: number
  last: boolean
  models: ModelEntry[]
  providers: Provider[]
  judgeRole?: RoleStatus
  onMove: (delta: number) => void
  onRemove: () => void
}) {
  const alias = id === JUDGE_ALIAS
  const model = alias ? undefined : models.find((m) => m.id === id)
  const issue = alias ? aliasIssue(judgeRole) : unusableReason(model, providers)

  return (
    <li className="relative flex gap-3">
      <div className="flex flex-col items-center">
        <span
          className={cn(
            "flex size-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold tabular-nums",
            index === 0 ? "border-primary bg-primary text-primary-foreground" : "bg-card text-foreground",
          )}
          aria-hidden="true"
        >
          {index + 1}
        </span>
        {!last ? <span className="my-1 w-px flex-1 bg-border" aria-hidden="true" /> : null}
      </div>
      <div className={cn("flex min-w-0 flex-1 flex-col gap-2", last ? "pb-0" : "pb-3")}>
        <div className={cn("flex min-w-0 items-center gap-2 rounded-xl border bg-card py-2 ps-3 pe-2", issue && "border-amber-500/40")}>
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="truncate font-mono text-sm font-medium">{id}</span>
              {alias ? <Badge variant="secondary">别名</Badge> : model ? <KindBadge kind={model.kind} /> : null}
              {index === 0 ? <Badge variant="outline">先问</Badge> : null}
              {last ? (
                <Badge variant="outline" className="gap-1">
                  <FlagIcon />
                  兜底
                </Badge>
              ) : null}
            </div>
            <span className="truncate text-xs text-muted-foreground">
              {alias
                ? aliasDescription(judgeRole)
                : model
                  ? `${model.provider_name} · ${model.model}${model.kind === "chat" ? " · 以老师身份按题作答" : ""}`
                  : "注册表里没有这个名字"}
            </span>
            {issue ? (
              <span className="flex items-center gap-1 text-xs text-amber-700 dark:text-amber-400">
                <TriangleAlertIcon className="size-3.5" aria-hidden="true" />
                {issue},网关会跳过它
              </span>
            ) : null}
          </div>
          <div className="flex shrink-0 items-center">
            <Button type="button" size="icon-sm" variant="ghost" aria-label={`把 ${id} 上移一位`} disabled={index === 0} onClick={() => onMove(-1)}>
              <ArrowUpIcon />
            </Button>
            <Button type="button" size="icon-sm" variant="ghost" aria-label={`把 ${id} 下移一位`} disabled={last} onClick={() => onMove(1)}>
              <ArrowDownIcon />
            </Button>
            <Button type="button" size="icon-sm" variant="ghost" aria-label={`从路由里移除 ${id}`} onClick={onRemove}>
              <Trash2Icon />
            </Button>
          </div>
        </div>
        <span className="flex items-center gap-1.5 ps-1 text-xs text-muted-foreground">
          <CornerDownRightIcon className="size-3.5" aria-hidden="true" />
          {last ? "最后一位:无论有没有把握,都返回它的答案" : "每道题都有把握就直接返回;否则升级给下一位"}
        </span>
      </div>
    </li>
  )
}

function aliasDescription(role?: RoleStatus) {
  if (!role) return "当前绑定的适用性判官"
  if (role.source === "db") return `当前绑定的适用性判官 → ${role.model_id}(${role.provider ?? ""} · ${role.model ?? ""})`
  if (role.source === "env") return `当前适用性判官来自环境变量 → ${role.model ?? ""}`
  return "当前绑定的适用性判官(尚未配置)"
}

function aliasIssue(role?: RoleStatus) {
  if (!role) return undefined
  if (role.error) return "适用性判官的绑定异常"
  if (role.source === "none") return "适用性判官未配置"
  return undefined
}
