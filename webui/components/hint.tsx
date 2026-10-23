"use client"

import { InfoIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"

/** 标题旁的小问号:长解释收在悬浮提示里,页面上不放说明文字。 */
export function Hint({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={label}
          className={cn("inline-flex shrink-0 text-muted-foreground transition-colors hover:text-foreground", className)}
        >
          <InfoIcon className="size-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-72 leading-relaxed">{children}</TooltipContent>
    </Tooltip>
  )
}
