"use client"

// 触发条件、字符串列表与领域的编辑器:新建表单和编辑表单共用。
import { PlusIcon, XIcon } from "lucide-react"
import { useId, useState, type KeyboardEvent } from "react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import type { PatternItem } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import { patternText, readPattern, regexError, samePattern } from "../lib/patterns"

function Chip({ children, regex, label, onRemove }: { children: React.ReactNode; regex?: boolean; label: string; onRemove: () => void }) {
  return (
    <span
      className={cn(
        "inline-flex h-6 max-w-full min-w-0 items-center gap-1 rounded-lg border bg-background pr-0.5 pl-1.5 text-xs",
        regex && "border-sky-500/30 bg-sky-500/5",
      )}
    >
      {children}
      <button
        type="button"
        aria-label={`移除 ${label}`}
        onClick={onRemove}
        className="inline-flex size-5 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
      >
        <XIcon className="size-3" aria-hidden="true" />
      </button>
    </span>
  )
}

export function PatternEditor({
  id,
  value,
  onChange,
  invalid,
}: {
  id?: string
  value: PatternItem[]
  onChange: (next: PatternItem[]) => void
  invalid?: boolean
}) {
  const [draft, setDraft] = useState("")
  const [type, setType] = useState<PatternItem["type"]>("keyword")
  const [message, setMessage] = useState<string | null>(null)

  function add() {
    const item = readPattern(draft, type)
    if (!item) return
    const error = item.type === "regex" ? regexError(item.value) : undefined
    if (error) return setMessage(error)
    if (value.some((existing) => samePattern(existing, item))) return setMessage(`「${item.value}」已经在列表里了`)
    onChange([...value, item])
    setDraft("")
    setMessage(null)
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return
    if (event.key === "Enter") {
      event.preventDefault()
      add()
    } else if (type === "keyword" && (event.key === "," || event.key === "，")) {
      event.preventDefault()
      add()
    } else if (event.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1))
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      {value.length ? (
        <div className="flex flex-wrap gap-1.5">
          {value.map((item, index) => (
            <Chip
              key={`${item.type}-${item.value}-${index}`}
              regex={item.type === "regex"}
              label={patternText(item)}
              onRemove={() => onChange(value.filter((_, i) => i !== index))}
            >
              <span className="truncate font-mono">{patternText(item)}</span>
            </Chip>
          ))}
        </div>
      ) : null}
      <div className="flex items-center gap-2">
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          spacing={0}
          value={type}
          onValueChange={(next) => next && setType(next as PatternItem["type"])}
          aria-label="条件类型"
        >
          <ToggleGroupItem value="keyword" className="px-2.5 text-xs">
            关键词
          </ToggleGroupItem>
          <ToggleGroupItem value="regex" className="px-2.5 text-xs">
            正则
          </ToggleGroupItem>
        </ToggleGroup>
        <Input
          id={id}
          value={draft}
          aria-invalid={invalid || Boolean(message) || undefined}
          placeholder={type === "regex" ? "Python re 语法,如 \\d{3}-\\d{4}" : "输入关键词,回车添加"}
          className="h-8 min-w-0 flex-1"
          onChange={(event) => {
            setDraft(event.target.value)
            setMessage(null)
          }}
          onKeyDown={onKeyDown}
        />
        <Button type="button" size="sm" variant="outline" disabled={!draft.trim()} onClick={add}>
          <PlusIcon /> 添加
        </Button>
      </div>
      {message ? <p className="text-xs text-destructive">{message}</p> : null}
    </div>
  )
}

export function ListEditor({
  id,
  value,
  onChange,
  label,
  placeholder,
  splitOnComma,
  layout = "chips",
  invalid,
}: {
  id?: string
  value: string[]
  onChange: (next: string[]) => void
  label: string
  placeholder: string
  splitOnComma?: boolean
  layout?: "chips" | "lines"
  invalid?: boolean
}) {
  const [draft, setDraft] = useState("")

  function add(text: string) {
    const parts = (splitOnComma ? text.split(/[,，]/) : [text]).map((part) => part.trim()).filter(Boolean)
    const next = [...value]
    for (const part of parts) if (!next.includes(part)) next.push(part)
    if (next.length !== value.length) onChange(next)
    setDraft("")
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.nativeEvent.isComposing) return
    if (event.key === "Enter" || (splitOnComma && (event.key === "," || event.key === "，"))) {
      event.preventDefault()
      add(draft)
    } else if (event.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1))
    }
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      {value.length ? (
        layout === "lines" ? (
          <ul className="flex flex-col gap-1">
            {value.map((item, index) => (
              <li key={`${item}-${index}`} className="flex items-center gap-2 rounded-lg border bg-muted/30 px-2 py-1 text-sm">
                <span className="min-w-0 flex-1 truncate" title={item}>
                  {item}
                </span>
                <button
                  type="button"
                  aria-label={`移除 ${item}`}
                  onClick={() => onChange(value.filter((_, i) => i !== index))}
                  className="inline-flex size-5 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                >
                  <XIcon className="size-3" aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {value.map((item, index) => (
              <Chip key={`${item}-${index}`} label={item} onRemove={() => onChange(value.filter((_, i) => i !== index))}>
                <span className="truncate">{item}</span>
              </Chip>
            ))}
          </div>
        )
      ) : null}
      <div className="flex items-center gap-2">
        <Input
          id={id}
          value={draft}
          aria-label={label}
          aria-invalid={invalid || undefined}
          placeholder={placeholder}
          className="h-8 min-w-0 flex-1"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <Button type="button" size="sm" variant="outline" disabled={!draft.trim()} onClick={() => add(draft)}>
          <PlusIcon /> 添加
        </Button>
      </div>
    </div>
  )
}

/** 领域输入:已有领域作候选,也可以填新的;留空表示通用。 */
export function DomainInput({
  id,
  value,
  onChange,
  domains,
  invalid,
}: {
  id?: string
  value: string
  onChange: (next: string) => void
  domains: Record<string, number>
  invalid?: boolean
}) {
  const listId = useId()
  return (
    <>
      <Input
        id={id}
        value={value}
        list={listId}
        autoComplete="off"
        placeholder="留空表示通用"
        aria-invalid={invalid || undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id={listId}>
        {Object.keys(domains)
          .filter(Boolean)
          .map((name) => (
            <option key={name} value={name} />
          ))}
      </datalist>
    </>
  )
}
