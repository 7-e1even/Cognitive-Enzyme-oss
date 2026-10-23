"use client"

import { CheckIcon, PencilIcon, XIcon } from "lucide-react"
import type { KeyboardEvent, ReactNode } from "react"

import { Hint } from "@/components/hint"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * 详情抽屉里可逐行编辑的一行:平时是「名称 → 值 + 铅笔」,点铅笔变成输入控件加保存 / 取消。
 * 每行单独保存,一次只改一个字段;Enter 保存(多行文本用 Enter,换行用 Shift+Enter),Esc 只退出这一行。
 * 编辑中的行带 data-editing,抽屉据此在 Esc 时不关闭自己。
 */
export function EditableRow({
  label,
  hint,
  align = "center",
  editing,
  saving,
  actionsDisabled,
  error,
  enterToSave,
  onEdit,
  onCancel,
  onSave,
  display,
  children,
}: {
  label: string
  hint?: string
  align?: "center" | "start"
  editing: boolean
  saving: boolean
  actionsDisabled: boolean
  error?: string
  enterToSave?: boolean
  onEdit: () => void
  onCancel: () => void
  onSave: () => void
  /** 非编辑态下显示的值 */
  display: ReactNode
  /** 编辑态下的输入控件 */
  children: ReactNode
}) {
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.nativeEvent.isComposing) return
    if (event.key === "Escape") {
      event.preventDefault()
      event.stopPropagation()
      onCancel()
      return
    }
    if (enterToSave && event.key === "Enter" && !event.shiftKey) {
      const target = event.target as HTMLElement
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
        event.preventDefault()
        onSave()
      }
    }
  }

  return (
    <div className={cn("group/row grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 px-4 py-1.5", align === "start" ? "items-start" : "items-center")}>
      <div className={cn("flex items-center gap-1 text-sm text-muted-foreground", align === "start" && "pt-1")}>
        <span>{label}</span>
        {hint ? <Hint label={label}>{hint}</Hint> : null}
      </div>
      {editing ? (
        <div data-editing="true" onKeyDown={onKeyDown} className="flex min-w-0 flex-col gap-1.5">
          <div className="flex items-start gap-1.5">
            <div className="min-w-0 flex-1">{children}</div>
            <Button type="button" size="icon-sm" aria-label={`保存${label}`} disabled={saving} onClick={onSave}>
              {saving ? <Spinner /> : <CheckIcon />}
            </Button>
            <Button type="button" size="icon-sm" variant="ghost" aria-label={`取消编辑${label}`} disabled={saving} onClick={onCancel}>
              <XIcon />
            </Button>
          </div>
          {error ? <p className="text-xs text-destructive">{error}</p> : null}
        </div>
      ) : (
        <div className="flex min-w-0 items-start justify-between gap-2">
          <div className="min-w-0 flex-1 py-0.5 text-sm">{display}</div>
          <Button
            type="button"
            size="icon-xs"
            variant="ghost"
            aria-label={`编辑${label}`}
            disabled={actionsDisabled}
            className="shrink-0 opacity-0 transition-opacity group-hover/row:opacity-100 focus-visible:opacity-100 max-md:opacity-100"
            onClick={onEdit}
          >
            <PencilIcon />
          </Button>
        </div>
      )}
    </div>
  )
}
