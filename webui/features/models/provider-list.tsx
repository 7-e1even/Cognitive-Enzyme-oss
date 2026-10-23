"use client"

import { DatabaseZapIcon, EllipsisVerticalIcon, PlugZapIcon, PlusIcon, SearchIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { ScrollArea } from "@/components/ui/scroll-area"
import type { ModelEntry, Provider } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { providerState, ProviderLogo, ProviderStateBadge } from "./bits"
import { useProviderProbe } from "./lib/queries"

/** 左栏:服务商清单。搜索同时匹配服务商和它下面的模型名;底部是「添加」和不常用的操作。 */
export function ProviderList({
  providers,
  models,
  selectedId,
  query,
  onQueryChange,
  onSelect,
  onAdd,
  onImport,
  onTestAll,
  importing,
  testingAll,
}: {
  providers: Provider[]
  models: ModelEntry[]
  selectedId: string | null
  query: string
  onQueryChange: (value: string) => void
  onSelect: (provider: Provider) => void
  onAdd: () => void
  onImport: () => void
  onTestAll: () => void
  importing: boolean
  testingAll: boolean
}) {
  const needle = query.trim().toLowerCase()
  const visible = needle
    ? providers.filter(
        (p) =>
          [p.name, p.id, p.base_url].some((field) => field.toLowerCase().includes(needle)) ||
          models.some(
            (m) =>
              m.provider_id === p.id &&
              (m.id.toLowerCase().includes(needle) || m.model.toLowerCase().includes(needle)),
          ),
      )
    : providers

  return (
    <div className="flex min-h-0 flex-col md:h-full">
      <div className="p-3">
        <InputGroup className="h-8">
          <InputGroupAddon>
            <SearchIcon />
          </InputGroupAddon>
          <InputGroupInput
            aria-label="搜索服务商或模型"
            placeholder="搜索服务商或模型"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
          />
        </InputGroup>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <nav aria-label="服务商" className="flex flex-col gap-0.5 px-2 pb-2">
          {visible.length === 0 ? (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">没有匹配的服务商</p>
          ) : (
            visible.map((provider) => (
              <ProviderListItem
                key={provider.id}
                provider={provider}
                modelCount={models.filter((m) => m.provider_id === provider.id).length}
                active={provider.id === selectedId}
                onSelect={() => onSelect(provider)}
              />
            ))
          )}
        </nav>
      </ScrollArea>
      <div className="flex items-center gap-2 border-t p-3">
        <Button size="sm" variant="outline" className="flex-1" onClick={onAdd}>
          <PlusIcon /> 添加
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label="更多操作">
              <EllipsisVerticalIcon aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuItem disabled={testingAll || providers.length === 0} onClick={onTestAll}>
              <PlugZapIcon aria-hidden="true" />
              {testingAll ? "测试中…" : "全部测试"}
            </DropdownMenuItem>
            <DropdownMenuItem disabled={importing} onClick={onImport}>
              <DatabaseZapIcon aria-hidden="true" />
              从环境变量导入
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  )
}

function ProviderListItem({
  provider,
  modelCount,
  active,
  onSelect,
}: {
  provider: Provider
  modelCount: number
  active: boolean
  onSelect: () => void
}) {
  // 只读缓存:这里不发请求,检测过的才显示「已连通 / 连接失败」
  const probe = useProviderProbe(provider.id)
  return (
    <button
      type="button"
      data-row-id={provider.id}
      aria-current={active ? "page" : undefined}
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-2.5 rounded-xl px-2 py-2 text-start transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
        active ? "bg-muted" : "hover:bg-muted/60",
      )}
    >
      <ProviderLogo name={provider.name} size="sm" />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex w-full min-w-0 items-center gap-2 text-sm">
          <span className={cn("min-w-0 truncate", !provider.enabled && "text-muted-foreground")}>{provider.name}</span>
          <ProviderStateBadge state={providerState(provider, probe.data)} />
        </span>
        <span className="truncate text-xs text-muted-foreground">
          {labelOf.protocol(provider.protocol).label} · {modelCount} 个模型
        </span>
      </span>
    </button>
  )
}
