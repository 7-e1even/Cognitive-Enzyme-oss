"use client"

import { ArrowRightIcon, CircleCheckIcon, InfoIcon } from "lucide-react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { ImportEnvResult } from "@/lib/api/types"

import { usageLabel } from "./lib/registry"

/** 「从环境变量导入」做完之后的交代;什么都没发生也要说,否则按钮看起来像坏了。 */
export function ImportEnvResultDialog({
  result,
  open,
  onOpenChange,
}: {
  result: ImportEnvResult | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const created = result?.created ?? []
  const providers = created.filter((c) => c.startsWith("provider:")).map((c) => c.slice("provider:".length))
  const models = created.filter((c) => c.startsWith("model:")).map((c) => c.slice("model:".length))
  const bindings = Object.entries(result?.bindings ?? {})
  const teachers = result?.teachers ?? []
  const nothing = created.length === 0 && bindings.length === 0 && teachers.length === 0

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {nothing ? <InfoIcon className="size-5 text-muted-foreground" /> : <CircleCheckIcon className="size-5 text-emerald-600" />}
            {nothing ? "没有需要导入的配置" : "导入完成"}
          </DialogTitle>
          <DialogDescription>
            {nothing
              ? "所有角色都已在后台绑定,或者环境变量里没有旧的模型配置与老师配置。"
              : "旧配置已写进注册表;同协议、同地址、同密钥的连接复用了已有服务商。"}
          </DialogDescription>
        </DialogHeader>
        {!nothing ? (
          <div className="flex flex-col gap-4 text-sm">
            <ResultSection title="新建服务商" empty="没有新建,全部复用已有服务商">
              {providers.map((id) => (
                <Badge key={id} variant="outline" className="font-mono">
                  {id}
                </Badge>
              ))}
            </ResultSection>
            <ResultSection title="新建模型" empty="没有新建模型">
              {models.map((id) => (
                <Badge key={id} variant="outline" className="font-mono">
                  {id}
                </Badge>
              ))}
            </ResultSection>
            <ResultSection title="角色绑定" empty="没有需要绑定的角色" column>
              {bindings.map(([role, model]) => (
                <span key={role} className="flex items-center gap-2">
                  <span className="w-24 shrink-0 text-muted-foreground">{usageLabel(role)}</span>
                  <ArrowRightIcon className="size-3.5 text-muted-foreground" aria-hidden="true" />
                  <span className="font-mono">{model}</span>
                </span>
              ))}
            </ResultSection>
            <ResultSection title="老师(判定标注)" empty="环境变量里没有老师配置">
              {teachers.map((id) => (
                <Badge key={id} variant="secondary" className="font-mono">
                  {id}
                </Badge>
              ))}
            </ResultSection>
          </div>
        ) : null}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" size="sm">
              知道了
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ResultSection({
  title,
  empty,
  column,
  children,
}: {
  title: string
  empty: string
  column?: boolean
  children: React.ReactNode[]
}) {
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {children.length > 0 ? (
        <div className={column ? "flex flex-col gap-1.5" : "flex flex-wrap gap-1.5"}>{children}</div>
      ) : (
        <p className="text-xs text-muted-foreground/80">{empty}</p>
      )}
    </section>
  )
}
