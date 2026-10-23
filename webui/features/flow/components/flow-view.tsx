"use client"

import { useQuery } from "@tanstack/react-query"
import type { NodeTypes } from "@xyflow/react"
import { useMemo, useState } from "react"

import { dashboardApi, enzymeApi } from "@/lib/api/endpoints"
import { qk, useIsAdmin } from "@/lib/api/hooks"

import { buildFlow, type FlowNode, type StepNodeData } from "../lib/steps"
import { FlowCanvas } from "./flow-canvas"
import { LabeledEdge } from "./flow-edge"
import { LaneNode } from "./lane-node"
import { StepNode } from "./step-node"
import { StepSheet } from "./step-sheet"

const NODE_TYPES: NodeTypes = { step: StepNode, lane: LaneNode }
const EDGE_TYPES = { labeled: LabeledEdge }

/** 按看的人所在时区切日,和首页用同一份缓存。 */
function viewerTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"
  } catch {
    return "UTC"
  }
}

/**
 * 工作流程:召回、学习、巩固三条线画在一张画布上,每条线的名字标在画布上。
 * 卡片和侧栏里的数字、开关、阈值都来自实际配置与实时统计;点一张卡片,右侧展开这一步的具体规则。
 */
export function FlowView() {
  const isAdmin = useIsAdmin()
  const tz = useMemo(() => viewerTimeZone(), [])
  // 记 id 不记数据:参数每分钟刷新,开着的侧栏也跟着更新
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const home = useQuery({ queryKey: [...qk.home, tz], queryFn: () => dashboardApi.home(tz), refetchInterval: 60_000 })
  const dashboard = useQuery({ queryKey: [...qk.dashboard, { days: 28 }], queryFn: () => dashboardApi.get(28) })
  const facts = useQuery({ queryKey: qk.flow, queryFn: dashboardApi.flow, refetchInterval: 60_000 })
  const facets = useQuery({ queryKey: qk.facets, queryFn: enzymeApi.facets })
  const domainCount = facets.data ? Object.keys(facets.data.domain).filter(Boolean).length : undefined

  const { nodes, edges } = useMemo(
    () => buildFlow(home.data, dashboard.data, facts.data, domainCount, isAdmin),
    [home.data, dashboard.data, facts.data, domainCount, isAdmin],
  )
  const selected = (nodes.find((node) => node.id === selectedId && node.type === "step")?.data ?? null) as StepNodeData | null

  return (
    <div className="flex min-h-[36rem] w-full min-w-0 flex-1 flex-col">
      <FlowCanvas<FlowNode, (typeof edges)[number]>
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        edgeTypes={EDGE_TYPES}
        onNodeClick={(_, node) => {
          if (node.type === "step") setSelectedId(node.id)
        }}
      />
      <StepSheet step={selected} onClose={() => setSelectedId(null)} />
    </div>
  )
}
