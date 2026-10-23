"use client"

import { CheckIcon, CopyIcon } from "lucide-react"
import { useState, type ComponentProps, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { copyText } from "@/lib/clipboard"

export function CopyButton({
  value,
  label,
  children,
  size = "icon-xs",
  variant = "ghost",
  className,
}: {
  value: string
  /** 提示里的名称,例如「令牌」 */
  label: string
  children?: ReactNode
  size?: ComponentProps<typeof Button>["size"]
  variant?: ComponentProps<typeof Button>["variant"]
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    if (await copyText(value, label, "")) {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }
  return (
    <Button type="button" size={size} variant={variant} className={className} aria-label={`复制${label}`} onClick={() => void copy()}>
      {copied ? <CheckIcon /> : <CopyIcon />}
      {children}
    </Button>
  )
}
