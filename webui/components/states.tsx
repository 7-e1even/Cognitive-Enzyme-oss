import { RefreshCwIcon, ServerCrashIcon, type LucideIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { errorMessage } from "@/lib/api/client"

export function ErrorState({
  error,
  onRetry,
  title = "读取失败",
}: {
  error: unknown
  onRetry?: () => void
  title?: string
}) {
  return (
    <Empty className="border">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <ServerCrashIcon />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{errorMessage(error)}</EmptyDescription>
      </EmptyHeader>
      {onRetry ? (
        <EmptyContent>
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCwIcon /> 重试
          </Button>
        </EmptyContent>
      ) : null}
    </Empty>
  )
}

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
}: {
  icon?: LucideIcon
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <Empty>
      <EmptyHeader>
        {Icon ? (
          <EmptyMedia variant="icon">
            <Icon />
          </EmptyMedia>
        ) : null}
        <EmptyTitle>{title}</EmptyTitle>
        {description ? <EmptyDescription>{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}
