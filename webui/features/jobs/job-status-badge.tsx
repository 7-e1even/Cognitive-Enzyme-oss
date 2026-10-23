import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { STATUS_DOT } from "./lib/data"

export function JobStatusBadge({ status }: { status: string }) {
  const label = labelOf.jobStatus(status)
  return (
    <ToneBadge tone={label.tone} className="gap-1.5">
      {status === "running" ? (
        <Spinner className="size-2.5" />
      ) : (
        <span className={cn("size-1.5 shrink-0 rounded-full", STATUS_DOT[status] ?? "bg-muted-foreground")} aria-hidden="true" />
      )}
      {label.label}
    </ToneBadge>
  )
}
