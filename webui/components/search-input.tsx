"use client"

import { SearchIcon, XIcon } from "lucide-react"
import { useRef, useState } from "react"

import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { cn } from "@/lib/utils"

/** 搜索框:输入停顿 300ms 后才通知外面;外面把值清空时输入框跟着清空。 */
export function SearchInput({
  value,
  onChange,
  placeholder,
  label = "搜索",
  delay = 300,
  className,
}: {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  label?: string
  delay?: number
  className?: string
}) {
  const [draft, setDraft] = useState(value)
  const [seen, setSeen] = useState(value)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 外面的值变了(清除筛选、后退)且不是自己刚发出去的,输入框对齐它
  if (seen !== value) {
    setSeen(value)
    if (value !== draft.trim()) setDraft(value)
  }

  function change(next: string) {
    setDraft(next)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => onChange(next.trim()), delay)
  }

  function clear() {
    if (timer.current) clearTimeout(timer.current)
    setDraft("")
    onChange("")
  }

  return (
    <InputGroup className={cn("h-8 w-full sm:w-64", className)}>
      <InputGroupAddon align="inline-start">
        <SearchIcon aria-hidden="true" />
      </InputGroupAddon>
      <InputGroupInput
        value={draft}
        placeholder={placeholder}
        aria-label={label}
        onChange={(event) => change(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape" && draft) {
            event.preventDefault()
            clear()
          }
        }}
      />
      {draft ? (
        <InputGroupAddon align="inline-end">
          <InputGroupButton size="icon-xs" aria-label="清除搜索" onClick={clear}>
            <XIcon aria-hidden="true" />
          </InputGroupButton>
        </InputGroupAddon>
      ) : null}
    </InputGroup>
  )
}
