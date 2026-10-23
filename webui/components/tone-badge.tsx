import type { ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

export type Tone =
  | "secondary"
  | "outline"
  | "primary-light"
  | "success-light"
  | "warning-light"
  | "info-light"
  | "destructive-light"

const TONE_CLASS: Record<Tone, string> = {
  secondary: "",
  outline: "",
  "primary-light": "bg-primary/10 text-foreground",
  "success-light": "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  "warning-light": "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  "info-light": "bg-sky-500/15 text-sky-700 dark:text-sky-400",
  "destructive-light": "",
}

/** labels.ts 里的色调 → shadcn Badge 的外观;状态一律用它,不在页面里手写颜色。 */
export function ToneBadge({
  tone = "outline",
  label,
  children,
  className,
  title,
}: {
  tone?: Tone
  label?: string
  children?: ReactNode
  className?: string
  title?: string
}) {
  const variant = tone === "outline" ? "outline" : tone === "destructive-light" ? "destructive" : "secondary"
  return (
    <Badge variant={variant} title={title} className={cn(TONE_CLASS[tone], className)}>
      {children ?? label}
    </Badge>
  )
}
