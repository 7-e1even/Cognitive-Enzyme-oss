import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, type EdgeProps } from "@xyflow/react"

/** 圆角折线,线上的文字(如「适用」「都不适用」)是一个小标签。 */
export function LabeledEdge({ id, label, markerEnd, sourcePosition, sourceX, sourceY, targetPosition, targetX, targetY }: EdgeProps) {
  const [path, labelX, labelY] = getSmoothStepPath({ borderRadius: 12, sourcePosition, sourceX, sourceY, targetPosition, targetX, targetY })
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} className="motion-reduce:animate-none!" />
      {label ? (
        <EdgeLabelRenderer>
          <span
            className="nodrag nopan absolute rounded-full border bg-background px-2 py-0.5 text-xs whitespace-nowrap text-muted-foreground"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {label}
          </span>
        </EdgeLabelRenderer>
      ) : null}
    </>
  )
}
