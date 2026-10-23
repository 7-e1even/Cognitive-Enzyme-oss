"use client"

import { PlusIcon, XIcon } from "lucide-react"
import { useState, type ReactNode } from "react"

import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import type { ModelEntry, ModelKind, Provider } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { KindBadge } from "./bits"
import { JUDGE_ALIAS, unusableReason } from "./lib/registry"

/**
 * 从注册表里挑模型:网关路由、影子判官与「模型列表」类运行参数共用。
 * 「jev」别名单列在最前——它不是一个模型,而是「当前绑定的适用性判官」。
 * 停用的模型照样能选(网关会跳过它),但标出来。
 */
export function ModelPicker({
  models,
  providers,
  kinds,
  allowAlias,
  exclude = [],
  onPick,
  trigger,
  align = "start",
}: {
  models: ModelEntry[]
  providers?: Provider[]
  /** 允许的模型类型;不给就是全部 */
  kinds?: ModelKind[]
  /** 列出「jev」别名 */
  allowAlias?: boolean
  /** 已经选了的,不再列出 */
  exclude?: string[]
  onPick: (id: string) => void
  trigger?: ReactNode
  align?: "start" | "center" | "end"
}) {
  const [open, setOpen] = useState(false)
  const taken = new Set(exclude)
  const options = models.filter((m) => (!kinds || kinds.includes(m.kind)) && !taken.has(m.id))
  const showAlias = allowAlias && !taken.has(JUDGE_ALIAS)
  const kindOrder: ModelKind[] = ["judge", "chat", "embedding", "rerank"]
  const groups = kindOrder.map((kind) => ({ kind, items: options.filter((m) => m.kind === kind) })).filter((group) => group.items.length > 0)
  const nothing = groups.length === 0 && !showAlias

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {trigger ?? (
          <Button type="button" size="sm" variant="outline">
            <PlusIcon /> 添加
          </Button>
        )}
      </PopoverTrigger>
      <PopoverContent align={align} className="w-80 gap-0 p-0">
        {nothing ? (
          <p className="p-4 text-sm text-muted-foreground">
            没有可添加的模型{kinds ? `(需要${kinds.map((k) => labelOf.kind(k).label).join(" / ")}类型)` : ""}。先到「服务商与模型」注册。
          </p>
        ) : (
          <Command>
            <CommandInput placeholder="搜索注册名或模型名" />
            <CommandList>
              <CommandEmpty>没有匹配的模型</CommandEmpty>
              {showAlias ? (
                <CommandGroup heading="别名">
                  <CommandItem
                    value={`${JUDGE_ALIAS} 当前适用性判官`}
                    onSelect={() => {
                      onPick(JUDGE_ALIAS)
                      setOpen(false)
                    }}
                  >
                    <span className="font-mono">{JUDGE_ALIAS}</span>
                    <span className="truncate text-xs text-muted-foreground">当前绑定的适用性判官</span>
                  </CommandItem>
                </CommandGroup>
              ) : null}
              {groups.map((group) => (
                <CommandGroup key={group.kind} heading={labelOf.kind(group.kind).label}>
                  {group.items.map((m) => {
                    const issue = unusableReason(m, providers)
                    return (
                      <CommandItem
                        key={m.id}
                        value={`${m.id} ${m.model} ${m.provider_name}`}
                        onSelect={() => {
                          onPick(m.id)
                          setOpen(false)
                        }}
                      >
                        <span className="flex min-w-0 flex-1 flex-col">
                          <span className={cn("truncate font-mono", issue && "text-muted-foreground")}>{m.id}</span>
                          <span className="truncate text-xs text-muted-foreground">
                            {m.provider_name} · {m.model}
                            {issue ? ` · ${issue}` : ""}
                          </span>
                        </span>
                      </CommandItem>
                    )
                  })}
                </CommandGroup>
              ))}
            </CommandList>
          </Command>
        )}
      </PopoverContent>
    </Popover>
  )
}

/** 一枚模型名:别名、存在与否、可用与否都在这枚徽章上说清。 */
export function ModelChip({
  id,
  models,
  providers,
  onRemove,
  className,
}: {
  id: string
  models: ModelEntry[]
  providers?: Provider[]
  onRemove?: () => void
  className?: string
}) {
  const model = models.find((m) => m.id === id)
  const alias = id === JUDGE_ALIAS
  const issue = alias ? undefined : unusableReason(model, providers)
  return (
    <Badge
      variant="outline"
      className={cn("h-7 max-w-full gap-1.5 ps-2 pe-1", issue && "border-amber-500/50 text-amber-700 dark:text-amber-400", className)}
      title={alias ? "当前绑定的适用性判官" : (issue ?? `${model?.provider_name} · ${model?.model}`)}
    >
      {model ? <KindBadge kind={model.kind} /> : null}
      <span className="truncate font-mono">{id}</span>
      {alias ? <span className="font-normal text-muted-foreground">别名</span> : null}
      {issue ? <span className="font-normal">{issue}</span> : null}
      {onRemove ? (
        <Button type="button" variant="ghost" size="icon-xs" className="size-5 rounded-full" aria-label={`移除 ${id}`} onClick={onRemove}>
          <XIcon />
        </Button>
      ) : null}
    </Badge>
  )
}

/** 有序的模型列表输入:徽章 + 添加。顺序即数组顺序,新加的排在最后。 */
export function ModelListInput({
  value,
  onChange,
  models,
  providers,
  kinds,
  allowAlias,
  invalid,
  emptyText = "未设置",
}: {
  value: string[]
  onChange: (value: string[]) => void
  models: ModelEntry[]
  providers?: Provider[]
  kinds?: ModelKind[]
  allowAlias?: boolean
  invalid?: boolean
  emptyText?: string
}) {
  return (
    <div
      className={cn(
        "flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-lg border border-input px-1.5 py-1 dark:bg-input/30",
        invalid && "border-destructive ring-3 ring-destructive/20",
      )}
    >
      {value.length === 0 ? <span className="px-1.5 text-sm text-muted-foreground">{emptyText}</span> : null}
      {value.map((id, index) => (
        <ModelChip key={`${id}-${index}`} id={id} models={models} providers={providers} onRemove={() => onChange(value.filter((_, i) => i !== index))} />
      ))}
      <ModelPicker
        models={models}
        providers={providers}
        kinds={kinds}
        allowAlias={allowAlias}
        exclude={value}
        onPick={(id) => onChange([...value, id])}
        trigger={
          <Button type="button" size="xs" variant="ghost" className="ms-auto">
            <PlusIcon /> 添加
          </Button>
        }
        align="end"
      />
    </div>
  )
}
