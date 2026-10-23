import { CircleAlertIcon, CircleCheckIcon, InfoIcon, TriangleAlertIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Alert, AlertAction, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { cn } from "@/lib/utils"

export type NoticeTone = "info" | "warning" | "success" | "destructive"

const TONE_CLASS: Record<NoticeTone, string> = {
  info: "border-sky-500/30 bg-sky-500/5 text-sky-800 dark:text-sky-300",
  warning: "border-amber-500/40 bg-amber-500/5 text-amber-800 dark:text-amber-300",
  success: "border-emerald-500/30 bg-emerald-500/5 text-emerald-800 dark:text-emerald-300",
  destructive: "border-destructive/30 bg-destructive/5",
}

const TONE_ICON = {
  info: InfoIcon,
  warning: TriangleAlertIcon,
  success: CircleCheckIcon,
  destructive: CircleAlertIcon,
}

/** 横幅提示:一个标题、可选说明与操作。只在有话要说时才渲染。 */
export function Notice({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: NoticeTone
  title: ReactNode
  children?: ReactNode
  action?: ReactNode
  className?: string
}) {
  const Icon = TONE_ICON[tone]
  return (
    <Alert variant={tone === "destructive" ? "destructive" : "default"} className={cn(TONE_CLASS[tone], action && "sm:pr-28", className)}>
      <Icon />
      <AlertTitle>{title}</AlertTitle>
      {children ? <AlertDescription className="text-current/80">{children}</AlertDescription> : null}
      {action ? <AlertAction className="top-2 right-2">{action}</AlertAction> : null}
    </Alert>
  )
}
