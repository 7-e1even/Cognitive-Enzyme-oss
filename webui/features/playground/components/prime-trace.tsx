import { AlertTriangleIcon, CheckIcon, MinusIcon, XIcon } from "lucide-react"

import { StepRail, StepRailItem } from "@/components/step-rail"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { PlaygroundResult } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import { buildStages, type StageState } from "./data"

const MARKER: Record<StageState, string> = {
  done: "border-primary bg-primary text-primary-foreground",
  warn: "border-amber-500/40 bg-amber-500/15 text-amber-700 dark:text-amber-400",
  failed: "border-destructive/40 bg-destructive/10 text-destructive",
  skipped: "bg-muted text-muted-foreground",
}

function StageIcon({ state }: { state: StageState }) {
  if (state === "done") return <CheckIcon aria-hidden="true" />
  if (state === "warn") return <AlertTriangleIcon aria-hidden="true" />
  if (state === "failed") return <XIcon aria-hidden="true" />
  return <MinusIcon aria-hidden="true" />
}

/** 流水线:每个阶段一行,下面一条「还剩多少候选」的比例条,六步连起来就是漏斗。 */
export function PrimeTrace({ result, className }: { result: PlaygroundResult; className?: string }) {
  const stages = buildStages(result)
  const base = Math.max(1, result.stages.recalled)
  const problems = stages.filter((stage) => stage.state === "failed" || stage.state === "warn").length

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>流水线</CardTitle>
        <CardDescription>
          召回 {result.stages.recalled} → 注入 {result.stages.injected}
          {problems ? `,${problems} 个阶段降级` : ",各阶段正常"}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <StepRail>
          {stages.map((stage) => (
            <StepRailItem key={stage.key} marker={<StageIcon state={stage.state} />} markerClassName={MARKER[stage.state]}>
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
                <span className="text-sm font-semibold">{stage.title}</span>
                <span
                  className={cn(
                    "text-xs font-medium tabular-nums",
                    stage.state === "failed" && "text-destructive",
                    stage.state === "warn" && "text-amber-700 dark:text-amber-400",
                    stage.state === "skipped" && "text-muted-foreground",
                  )}
                >
                  {stage.countLabel}
                </span>
              </div>
              <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
                {stage.count !== null ? (
                  <div
                    className={cn("h-full rounded-full", stage.state === "done" ? "bg-primary" : "bg-muted-foreground")}
                    style={{ width: `${Math.min(100, (stage.count / base) * 100)}%` }}
                  />
                ) : null}
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{stage.detail}</p>
            </StepRailItem>
          ))}
        </StepRail>
      </CardContent>
    </Card>
  )
}
