import Link from "next/link"
import type { ReactNode } from "react"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/** 一格指标:图标 + 名称、大数字、一句补充;给了 href 整张卡可点。 */
export function MetricCard({
  icon,
  label,
  value,
  detail,
  href,
  warn,
  children,
}: {
  icon?: ReactNode
  label: string
  value: ReactNode
  detail?: ReactNode
  href?: string
  warn?: boolean
  children?: ReactNode
}) {
  const body = (
    <Card className={cn("h-full", href && "transition-colors hover:bg-muted/40")}>
      <CardHeader>
        <CardDescription className="flex items-center gap-2 [&_svg]:size-4">
          {icon}
          {label}
        </CardDescription>
        <CardTitle className="text-xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      {detail || children ? (
        <CardContent className="flex flex-col gap-2">
          {detail ? <div className={cn("text-sm", warn ? "text-destructive" : "text-muted-foreground")}>{detail}</div> : null}
          {children}
        </CardContent>
      ) : null}
    </Card>
  )
  return href ? <Link href={href}>{body}</Link> : body
}
