import type { NodeProps } from "@xyflow/react"

import type { LaneNodeType } from "../lib/steps"

/** 一条线的名字,标在这条线第一张卡片上方:召回 · 每次提问。 */
export function LaneNode({ data }: NodeProps<LaneNodeType>) {
  return (
    <div className="pointer-events-none flex items-baseline gap-2 whitespace-nowrap select-none">
      <span className="text-sm font-semibold">{data.title}</span>
      <span className="text-xs text-muted-foreground">{data.cadence}</span>
    </div>
  )
}
