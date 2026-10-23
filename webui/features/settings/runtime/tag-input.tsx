"use client"

import { XIcon } from "lucide-react"
import { useState } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * 字符串列表:回车或逗号加一项,退格删掉最后一项,粘贴一串逗号分隔的也能拆开。
 * 与后端 coerce() 一样去掉首尾空白、丢弃空项;重复项不加。
 */
export function TagInput({
  id,
  value,
  onChange,
  placeholder = "输入后回车",
  disabled,
  invalid,
}: {
  id?: string
  value: string[]
  onChange: (value: string[]) => void
  placeholder?: string
  disabled?: boolean
  invalid?: boolean
}) {
  const [text, setText] = useState("")

  function add(raw: string) {
    const parts = raw
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
    if (parts.length === 0) return
    const next = [...value]
    for (const part of parts) if (!next.includes(part)) next.push(part)
    onChange(next)
    setText("")
  }

  return (
    <div
      className={cn(
        "flex min-h-8 w-full flex-wrap items-center gap-1.5 rounded-lg border border-input px-1.5 py-1 focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 dark:bg-input/30",
        invalid && "border-destructive ring-3 ring-destructive/20",
        disabled && "pointer-events-none opacity-50",
      )}
    >
      {value.map((item, index) => (
        <Badge key={`${item}-${index}`} variant="outline" className="h-6 max-w-full gap-1 ps-2 pe-0.5">
          <span className="truncate font-mono">{item}</span>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="size-5 rounded-full"
            aria-label={`移除 ${item}`}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            <XIcon />
          </Button>
        </Badge>
      ))}
      <input
        id={id}
        value={text}
        disabled={disabled}
        placeholder={value.length === 0 ? placeholder : ""}
        className="min-w-24 flex-1 bg-transparent px-1 text-sm outline-none placeholder:text-muted-foreground"
        onChange={(event) => {
          const next = event.target.value
          if (next.includes(",")) add(next)
          else setText(next)
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault()
            add(text)
          } else if (event.key === "Backspace" && !text && value.length > 0) {
            onChange(value.slice(0, -1))
          }
        }}
        onBlur={() => add(text)}
      />
    </div>
  )
}
