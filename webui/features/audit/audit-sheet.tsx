"use client"

import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"
import type { ReactNode } from "react"

import { JsonView } from "@/components/json-view"
import { ToneBadge } from "@/components/tone-badge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { avatarSrc } from "@/features/users/lib/users"
import { useRoles, useSettings } from "@/lib/api/hooks"
import type { AuditEntry, AdminUser } from "@/lib/api/types"
import { formatDateTime, formatRelative, initials } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import {
  actionLabel,
  actionTone,
  categoryMeta,
  categoryOf,
  changesOf,
  detailKeyLabel,
  formatDetailValue,
  isPlainObject,
  isSimpleValue,
  targetHref,
  TONE_DOT,
  TONE_LABEL,
  type SettingChange,
} from "./lib/audit"

function FactRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">{value}</dd>
    </div>
  )
}

/** 明细里的一个值:简单值说人话,列表拆成小标签,结构体给 JSON。 */
function ValueView({ field, value }: { field: string; value: unknown }) {
  if (value === null || value === undefined || value === "") return <span className="text-muted-foreground">(空)</span>
  if (Array.isArray(value) && isSimpleValue(value)) {
    if (value.length === 0) return <span className="text-muted-foreground">(空列表)</span>
    return (
      <span className="flex flex-wrap gap-1">
        {value.map((item, index) => (
          <Badge key={index} variant="outline" className="max-w-full font-mono font-normal">
            <span className="truncate">{String(item)}</span>
          </Badge>
        ))}
      </span>
    )
  }
  if (isSimpleValue(value)) return <span className="font-mono text-xs break-all">{formatDetailValue(field, value)}</span>
  return <JsonView value={value} className="max-h-48 text-[11px]" />
}

/** 运行参数与模型绑定的变更:逐项 修改前 → 修改后,修改前的值注明来自后台 / 环境变量 / 默认。 */
function ChangesTable({ entry, changes }: { entry: AuditEntry; changes: SettingChange[] }) {
  const isBinding = entry.action === "model.bind"
  // 只有打开了这类记录才去取参数表 / 角色表,拿来把 key 翻成名字
  const settings = useSettings()
  const roles = useRoles()
  const labelFor = (key: string) =>
    isBinding ? roles.data?.roles.find((role) => role.key === key)?.label : settings.data?.settings.find((item) => item.key === key)?.label
  return (
    <div className="overflow-hidden rounded-xl border">
      <table className="w-full table-fixed text-sm">
        <thead className="bg-muted/40 text-xs text-muted-foreground">
          <tr>
            <th className="w-[34%] px-3 py-2 text-left font-medium">{isBinding ? "角色" : "参数"}</th>
            <th className="px-3 py-2 text-left font-medium">修改前</th>
            <th className="w-6 px-0 py-2" aria-hidden="true" />
            <th className="px-3 py-2 text-left font-medium">修改后</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {changes.map((change) => {
            const source = change.before_source ? labelOf.source(change.before_source) : null
            const label = labelFor(change.key)
            return (
              <tr key={change.key} className="align-top">
                <td className="px-3 py-2.5">
                  <div className="flex min-w-0 flex-col gap-0.5">
                    {label ? <span className="truncate text-sm">{label}</span> : null}
                    <span className={cn("font-mono break-all", label ? "text-[11px] text-muted-foreground" : "text-xs")}>{change.key}</span>
                  </div>
                </td>
                <td className="px-3 py-2.5">
                  <div className="flex min-w-0 flex-col items-start gap-1">
                    <span className="text-muted-foreground line-through decoration-current/40">
                      <ValueView field={change.key} value={change.before} />
                    </span>
                    {source ? <ToneBadge tone={source.tone} label={source.label} /> : null}
                  </div>
                </td>
                <td className="px-0 py-2.5 text-center text-muted-foreground">
                  <ArrowRightIcon className="inline size-3.5" aria-hidden="true" />
                </td>
                <td className="px-3 py-2.5">
                  <ValueView field={change.key} value={change.after} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** 其余动作的明细:扁平字段逐行列出,嵌套结构整体交给 JSON 视图。 */
function DetailFields({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail)
  const simple = entries.filter(([, value]) => isSimpleValue(value) || (Array.isArray(value) && value.length <= 40))
  if (entries.length === 0) return <p className="py-6 text-center text-sm text-muted-foreground">这条记录没有附带明细。</p>
  if (simple.length !== entries.length) return <JsonView value={detail} />
  return (
    <dl className="divide-y rounded-xl border px-3">
      {simple.map(([key, value]) => (
        <div key={key} className="grid grid-cols-[8rem_1fr] gap-x-4 py-2.5">
          <dt className="flex min-w-0 flex-col text-sm text-muted-foreground">
            <span className="truncate">{detailKeyLabel(key)}</span>
            {detailKeyLabel(key) !== key ? <span className="truncate font-mono text-[11px]">{key}</span> : null}
          </dt>
          <dd className="min-w-0 text-sm">
            <ValueView field={key} value={value} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** 审计详情抽屉:身份头、「明细 / 原始数据」两页、底部两个顺手的筛选。 */
export function AuditSheet({
  entry,
  user,
  open,
  onOpenChange,
  onFilterActor,
  onFilterCategory,
}: {
  entry: AuditEntry | null
  /** 操作人对应的账号(账号被删了就没有) */
  user?: AdminUser
  open: boolean
  onOpenChange: (open: boolean) => void
  onFilterActor: (actor: string) => void
  onFilterCategory: (category: string) => void
}) {
  const tone = entry ? actionTone(entry.action) : "neutral"
  const category = entry ? categoryMeta(categoryOf(entry.action)) : null
  const changes = entry ? changesOf(entry) : null
  const detail = entry && isPlainObject(entry.detail) ? entry.detail : {}
  const href = entry ? targetHref(entry) : null
  const CategoryIcon = category?.icon
  const actorName = user?.display_name || entry?.actor || "系统"

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex flex-col gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-lg">
        <SheetHeader className="shrink-0 gap-0 border-b p-0">
          <SheetTitle className="sr-only">审计事件</SheetTitle>
          <SheetDescription className="sr-only">查看这条审计记录的操作人、时间、对象与完整明细。</SheetDescription>
          {entry ? (
            <div className="flex min-w-0 flex-col gap-2.5 px-5 py-4 pe-12">
              <div className="flex min-w-0 items-center gap-2">
                <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full", TONE_DOT[tone])} />
                <h2 className="min-w-0 flex-1 truncate text-lg font-semibold tracking-tight">{actionLabel(entry.action)}</h2>
                <Badge variant="outline" className="tabular-nums">
                  #{entry.id}
                </Badge>
              </div>
              <p className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
                <span className="font-mono">{entry.action}</span>
                <span>·</span>
                <span>{TONE_LABEL[tone]}</span>
                <span>·</span>
                <span>记录于 {formatRelative(entry.created_at)}</span>
              </p>
            </div>
          ) : null}
        </SheetHeader>

        {entry ? (
          <Tabs key={entry.id} defaultValue="detail" className="flex min-h-0 flex-1 flex-col gap-0">
            <div className="shrink-0 border-b px-5 py-2">
              <TabsList>
                <TabsTrigger value="detail">明细</TabsTrigger>
                <TabsTrigger value="raw">原始数据</TabsTrigger>
              </TabsList>
            </div>
            <ScrollArea className="min-h-0 flex-1">
              <TabsContent value="detail" className="mt-0">
                <div className="flex flex-col gap-5 px-5 py-5">
                  <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-3">
                    <FactRow
                      label="操作人"
                      value={
                        <span className="flex min-w-0 items-center gap-2">
                          <Avatar className="size-6">
                            {user?.avatar_url ? <AvatarImage src={avatarSrc(user.avatar_url)} alt={actorName} /> : null}
                            <AvatarFallback className="text-[10px]">{initials(actorName)}</AvatarFallback>
                          </Avatar>
                          <span className="truncate">{actorName}</span>
                          {entry.actor ? <span className="truncate font-mono text-xs text-muted-foreground">@{entry.actor}</span> : null}
                        </span>
                      }
                    />
                    <FactRow label="分类" value={category && CategoryIcon ? <span className="inline-flex items-center gap-1.5"><CategoryIcon className="size-4 text-muted-foreground" aria-hidden="true" />{category.label}</span> : null} />
                    <FactRow
                      label="对象"
                      value={
                        entry.target ? (
                          href ? (
                            <Link href={href} className="inline-flex items-center gap-1 font-mono text-xs break-all underline-offset-4 hover:underline">
                              {entry.target}
                              <ArrowRightIcon className="size-3 shrink-0" aria-hidden="true" />
                            </Link>
                          ) : (
                            <span className="font-mono text-xs break-all">{entry.target}</span>
                          )
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )
                      }
                    />
                    <FactRow label="时间" value={formatDateTime(entry.created_at)} />
                  </dl>
                  {changes ? <ChangesTable entry={entry} changes={changes} /> : <DetailFields detail={detail} />}
                </div>
              </TabsContent>
              <TabsContent value="raw" className="mt-0">
                <div className="px-5 py-5">
                  <JsonView value={entry} className="max-h-[60svh]" />
                </div>
              </TabsContent>
            </ScrollArea>
          </Tabs>
        ) : null}

        <SheetFooter className="shrink-0 flex-row flex-wrap items-center justify-between gap-2 border-t bg-muted/40 px-5 py-3">
          <Button type="button" size="sm" variant="outline" disabled={!entry?.actor} onClick={() => entry?.actor && onFilterActor(entry.actor)}>
            只看此操作人
          </Button>
          <Button type="button" size="sm" variant="outline" disabled={!entry} onClick={() => entry && onFilterCategory(categoryOf(entry.action))}>
            只看此分类
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
