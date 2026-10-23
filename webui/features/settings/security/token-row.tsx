"use client"

import { EyeIcon, EyeOffIcon, KeyRoundIcon, PencilLineIcon, RefreshCwIcon, Trash2Icon } from "lucide-react"
import type { ReactNode } from "react"

import { CopyButton } from "@/components/copy-button"
import { Spinner } from "@/components/spinner"
import { ToneBadge, type Tone } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

/** 前六后四,足够在日志里认出是哪一枚,又不至于泄露。 */
function mask(token: string) {
  if (token.length <= 12) return "•".repeat(Math.max(token.length, 8))
  return `${token.slice(0, 6)}${"•".repeat(12)}${token.slice(-4)}`
}

function RowAction({
  label,
  children,
  onClick,
  disabled,
  destructive,
}: {
  label: string
  children: ReactNode
  onClick: () => void
  disabled?: boolean
  destructive?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label={label}
          disabled={disabled}
          className={destructive ? "text-destructive hover:text-destructive" : undefined}
          onClick={onClick}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  )
}

/** 一枚机器令牌:名称 + 状态 + 用途,一行掩码输入框,旁边是复制、显示、轮换、自定义、清除。 */
export function TokenRow({
  label,
  status,
  meta,
  note,
  token,
  revealed,
  busy,
  rotating,
  onToggleReveal,
  onRotate,
  onCustom,
  onClear,
}: {
  label: string
  status: { label: string; tone: Tone }
  meta: string[]
  note?: string
  token: string
  revealed: boolean
  busy: boolean
  rotating: boolean
  onToggleReveal: () => void
  onRotate: () => void
  onCustom: () => void
  onClear: () => void
}) {
  return (
    <div className="flex flex-col gap-3 px-6 py-4 lg:flex-row lg:items-center lg:gap-6">
      <div className="flex min-w-0 flex-col gap-1 lg:w-72 lg:shrink-0">
        <div className="flex flex-wrap items-center gap-2">
          <KeyRoundIcon className="size-4 text-muted-foreground" aria-hidden="true" />
          <span className="text-sm font-medium">{label}</span>
          <ToneBadge tone={status.tone} label={status.label} />
        </div>
        <p className="text-xs text-muted-foreground">{meta.join(" · ")}</p>
        {note ? <p className="text-xs text-muted-foreground">{note}</p> : null}
      </div>
      <div className="flex min-w-0 flex-1 items-center gap-1">
        <Input
          readOnly
          aria-label={label}
          value={token ? (revealed ? token : mask(token)) : ""}
          placeholder="未设置"
          className="min-w-0 flex-1 font-mono text-xs"
        />
        {token ? <CopyButton value={token} label={label} size="icon-sm" /> : null}
        {token ? (
          <RowAction label={revealed ? "隐藏令牌" : "显示令牌"} onClick={onToggleReveal}>
            {revealed ? <EyeOffIcon /> : <EyeIcon />}
          </RowAction>
        ) : null}
        <RowAction label={token ? "轮换(生成新令牌)" : "生成令牌"} onClick={onRotate} disabled={busy}>
          {rotating ? <Spinner /> : <RefreshCwIcon />}
        </RowAction>
        <RowAction label="自定义令牌" onClick={onCustom} disabled={busy}>
          <PencilLineIcon />
        </RowAction>
        {token ? (
          <RowAction label="清除令牌" onClick={onClear} disabled={busy} destructive>
            <Trash2Icon />
          </RowAction>
        ) : null}
      </div>
    </div>
  )
}
