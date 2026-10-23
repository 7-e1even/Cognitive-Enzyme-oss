import type { ReactNode } from "react"

import { Hint } from "@/components/hint"
import { cn } from "@/lib/utils"

/** 抽屉里的分节标题。 */
export function DetailSection({ children, action, className }: { children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={cn("flex items-center justify-between gap-2 px-4 pt-5 pb-1.5", className)}>
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground">{children}</h3>
      {action}
    </div>
  )
}

/** 一行「名称 → 值」;名称旁可挂一个问号提示。 */
export function DetailRow({
  label,
  hint,
  align = "center",
  children,
}: {
  label: string
  hint?: string
  align?: "center" | "start"
  children: ReactNode
}) {
  return (
    <div className={cn("grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 px-4 py-1.5", align === "start" ? "items-start" : "items-center")}>
      <div className={cn("flex items-center gap-1 text-sm text-muted-foreground", align === "start" && "pt-0.5")}>
        <span>{label}</span>
        {hint ? <Hint label={label}>{hint}</Hint> : null}
      </div>
      <div className="min-w-0 text-sm">{children}</div>
    </div>
  )
}
