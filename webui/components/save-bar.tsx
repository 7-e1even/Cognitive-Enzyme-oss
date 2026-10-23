"use client"

import { RotateCcwIcon, SaveIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"

/** 粘在底部的保存条:有改动才出现。 */
export function SaveBar({
  count,
  saving,
  onDiscard,
  onSave,
  note,
}: {
  count: number
  saving?: boolean
  onDiscard: () => void
  onSave: () => void
  note?: ReactNode
}) {
  if (count === 0) return null
  return (
    <div
      role="region"
      aria-label="未保存的修改"
      className="sticky bottom-4 z-20 mx-auto flex w-full max-w-2xl flex-wrap items-center justify-between gap-3 rounded-xl border bg-background px-4 py-2.5 shadow-lg"
    >
      <span className="flex min-w-0 flex-wrap items-center gap-x-2 text-sm">
        <span>
          <span className="font-medium tabular-nums">{count}</span> 项修改未保存
        </span>
        {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
      </span>
      <div className="flex items-center gap-2">
        <Button type="button" size="sm" variant="ghost" disabled={saving} onClick={onDiscard}>
          <RotateCcwIcon /> 放弃
        </Button>
        <Button type="button" size="sm" disabled={saving} onClick={onSave}>
          {saving ? <Spinner /> : <SaveIcon />} 保存
        </Button>
      </div>
    </div>
  )
}
