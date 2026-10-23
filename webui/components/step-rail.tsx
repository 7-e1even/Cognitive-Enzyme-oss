import type { ReactNode } from "react"

import { cn } from "@/lib/utils"

/** 竖向步骤轨:左边一条细线串起圆形标记,右边是每一步的内容。 */
export function StepRail({ children, className }: { children: ReactNode; className?: string }) {
  return <ol className={cn("flex flex-col", className)}>{children}</ol>
}

export function StepRailItem({
  marker,
  markerClassName,
  children,
  className,
}: {
  marker: ReactNode
  markerClassName?: string
  children: ReactNode
  className?: string
}) {
  return (
    <li className={cn("group/rail relative pb-6 pl-10 last:pb-0", className)}>
      <span aria-hidden="true" className="absolute top-8 -bottom-0.5 left-[13.5px] w-px bg-border group-last/rail:hidden" />
      <span
        className={cn(
          "absolute top-0 left-0 flex size-7 items-center justify-center rounded-full border bg-background text-muted-foreground [&_svg]:size-3.5",
          markerClassName,
        )}
      >
        {marker}
      </span>
      {children}
    </li>
  )
}
