import { Handle, Position, type NodeProps } from "@xyflow/react"
import { CircleCheckIcon, CircleDotIcon, MinusIcon, TriangleAlertIcon } from "lucide-react"
import type { ReactNode } from "react"

import { ToneBadge, type Tone } from "@/components/tone-badge"
import { cn } from "@/lib/utils"

import type { StepNodeType, StepStatus } from "../lib/steps"

export const STATUS: Record<StepStatus, { label: string; tone: Tone; icon: ReactNode }> = {
  ok: { label: "正常", tone: "success-light", icon: <CircleCheckIcon aria-hidden="true" /> },
  warn: { label: "待处理", tone: "warning-light", icon: <TriangleAlertIcon aria-hidden="true" /> },
  off: { label: "未配置", tone: "outline", icon: <MinusIcon aria-hidden="true" /> },
  idle: { label: "未运行", tone: "outline", icon: <CircleDotIcon aria-hidden="true" /> },
}

/** 一个步骤:图标 + 名称 + 一行说明,底部是这一步的实时数字和状态。点卡片在侧栏里看具体规则。 */
export function StepNode({ data }: NodeProps<StepNodeType>) {
  const status = STATUS[data.status]
  return (
    <div className="w-60 cursor-pointer rounded-lg border bg-card text-card-foreground shadow-xs transition-colors hover:border-primary/60">
      {data.kind !== "trigger" ? <Handle type="target" position={Position.Left} isConnectable={false} /> : null}
      <div className="flex items-center gap-2 p-2">
        <span
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-md border [&_svg]:size-4",
            data.kind === "trigger" ? "border-primary bg-primary text-primary-foreground" : "bg-muted text-foreground",
          )}
        >
          {data.icon}
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm leading-4 font-medium">{data.title}</div>
          <div className="line-clamp-1 text-xs leading-4 text-muted-foreground">{data.detail}</div>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 border-t px-2 py-1.5">
        <span className="min-w-0 truncate text-xs text-muted-foreground tabular-nums">{data.metric}</span>
        <ToneBadge tone={status.tone} className="shrink-0">
          {status.icon}
          {data.statusLabel ?? status.label}
        </ToneBadge>
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  )
}
