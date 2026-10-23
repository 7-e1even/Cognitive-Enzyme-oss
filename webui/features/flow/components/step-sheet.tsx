"use client"

import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"

import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

import type { StepNodeData } from "../lib/steps"
import { STATUS } from "./step-node"

/** 点一张卡片,右侧展开这一步的具体规则(按实际配置写),底部去对应的页面。 */
export function StepSheet({ step, onClose }: { step: StepNodeData | null; onClose: () => void }) {
  const status = step ? STATUS[step.status] : null
  return (
    <Sheet open={step !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-md">
        {step && status ? (
          <>
            <SheetHeader className="gap-3 border-b pr-12">
              <div className="flex items-start gap-3">
                <span
                  className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-md border [&_svg]:size-4",
                    step.kind === "trigger" ? "border-primary bg-primary text-primary-foreground" : "bg-muted",
                  )}
                >
                  {step.icon}
                </span>
                <div className="min-w-0 flex-1">
                  <SheetTitle className="truncate text-sm">{step.title}</SheetTitle>
                  <SheetDescription className="text-xs">{step.detail}</SheetDescription>
                </div>
              </div>
              <div className="flex min-w-0 items-center gap-2">
                <ToneBadge tone={status.tone} className="shrink-0">
                  {status.icon}
                  {step.statusLabel ?? status.label}
                </ToneBadge>
                {step.metric ? (
                  <span className="min-w-0 truncate text-xs text-muted-foreground tabular-nums" title={step.metric}>
                    {step.metric}
                  </span>
                ) : null}
              </div>
            </SheetHeader>

            <div className="min-h-0 flex-1">
              <ScrollArea className="h-full">
                <h3 className="px-4 pt-5 pb-1.5 text-xs font-medium tracking-wide text-muted-foreground">这一步怎么做</h3>
                <ol className="flex list-decimal flex-col gap-2.5 pr-4 pb-5 pl-9 text-sm leading-6 text-muted-foreground marker:text-xs">
                  {step.rules.map((rule) => (
                    <li key={rule} className="pl-1">
                      {rule}
                    </li>
                  ))}
                </ol>
              </ScrollArea>
            </div>

            <SheetFooter className="flex-row items-center justify-end border-t">
              <Button type="button" size="sm" variant="outline" onClick={onClose}>
                关闭
              </Button>
              <Button size="sm" asChild>
                <Link href={step.href}>
                  {step.linkLabel}
                  <ArrowRightIcon />
                </Link>
              </Button>
            </SheetFooter>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  )
}
