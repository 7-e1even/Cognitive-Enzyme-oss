"use client"

import { Background, BackgroundVariant, Controls, ReactFlow, type Edge, type Node, type ReactFlowProps } from "@xyflow/react"
import { useTheme } from "next-themes"
import type { CSSProperties } from "react"

import "@xyflow/react/dist/style.css"

/** 画布颜色跟着站点主题走:xyflow 用 --xy-* 变量上色,这里映射到 shadcn 的语义色。 */
const THEME_VARS = {
  "--xy-background-color": "transparent",
  "--xy-edge-stroke": "var(--muted-foreground)",
  "--xy-edge-stroke-width": 1,
  "--xy-controls-button-background-color": "var(--card)",
  "--xy-controls-button-background-color-hover": "var(--muted)",
  "--xy-controls-button-border-color": "var(--border)",
  "--xy-controls-button-color": "var(--foreground)",
  "--xy-attribution-background-color": "transparent",
} as CSSProperties

/** 只读画布:不能拖、不能连、不能选,可以平移缩放。 */
export function FlowCanvas<N extends Node, E extends Edge>({ children, ...props }: ReactFlowProps<N, E>) {
  const { resolvedTheme } = useTheme()
  return (
    <div className="relative min-h-0 flex-1 overflow-hidden rounded-xl border bg-muted/30" style={THEME_VARS}>
      <ReactFlow<N, E>
        fitView
        fitViewOptions={{ padding: 0.1, maxZoom: 1 }}
        minZoom={0.25}
        maxZoom={2}
        zoomOnDoubleClick={false}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        colorMode={resolvedTheme === "dark" ? "dark" : "light"}
        proOptions={{ hideAttribution: true }}
        {...props}
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1.5} />
        <Controls position="bottom-left" showInteractive={false} />
        {children}
      </ReactFlow>
    </div>
  )
}
