"use client"

import { CircleCheckIcon, PlusIcon, SearchIcon, XIcon } from "lucide-react"
import { useMemo, useState } from "react"

import { Notice } from "@/components/notice"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import type { ProbeResult } from "@/lib/api/types"
import { formatRelative } from "@/lib/format"
import { cn } from "@/lib/utils"

const PREVIEW = 24

/** 一次连通性测试的结论:通没通、多快、服务商怎么说,以及它列出的模型(点一下就带进「新建模型」)。 */
export function ProbeResultPanel({
  result,
  testedAt,
  registered,
  onPickModel,
  onDismiss,
}: {
  result: ProbeResult
  testedAt?: number
  /** 这个服务商下已注册的服务侧模型名 */
  registered: Set<string>
  onPickModel?: (model: string) => void
  onDismiss?: () => void
}) {
  const models = useMemo(() => result.models ?? [], [result.models])
  const [expanded, setExpanded] = useState(false)
  const [filter, setFilter] = useState("")
  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase()
    const matched = q ? models.filter((m) => m.toLowerCase().includes(q)) : models
    return expanded || q ? matched : matched.slice(0, PREVIEW)
  }, [expanded, filter, models])

  return (
    <Notice
      tone={result.ok ? "success" : "destructive"}
      title={
        <span className="flex flex-wrap items-center gap-2">
          {result.ok ? "连接成功" : "连接失败"}
          <Badge variant="outline" className="tabular-nums">
            {result.latency_ms} ms
          </Badge>
          {testedAt ? (
            <span className="text-xs font-normal text-muted-foreground">
              {formatRelative(new Date(testedAt).toISOString())}测试
            </span>
          ) : null}
        </span>
      }
      action={
        onDismiss ? (
          <Button type="button" variant="ghost" size="icon-xs" aria-label="关闭测试结果" onClick={onDismiss}>
            <XIcon />
          </Button>
        ) : undefined
      }
    >
      <p className="font-mono text-xs break-all whitespace-pre-wrap">{result.detail}</p>
      {models.length > 0 ? (
        <div className="mt-2 flex w-full min-w-0 flex-col gap-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium text-foreground">
              服务商列出 {models.length} 个模型{onPickModel ? ",点一下即可注册" : ""}
            </span>
            {models.length > PREVIEW ? (
              <InputGroup className="h-7 w-48">
                <InputGroupAddon>
                  <SearchIcon />
                </InputGroupAddon>
                <InputGroupInput
                  aria-label="筛选模型"
                  placeholder="筛选模型名"
                  value={filter}
                  onChange={(event) => setFilter(event.target.value)}
                  className="text-xs"
                />
              </InputGroup>
            ) : null}
          </div>
          <div className="flex max-h-48 flex-wrap gap-1.5 overflow-y-auto pr-1">
            {visible.map((model) => {
              const taken = registered.has(model)
              return (
                <Button
                  key={model}
                  type="button"
                  size="xs"
                  variant="outline"
                  disabled={!onPickModel}
                  title={taken ? `${model}(已注册)` : `注册 ${model}`}
                  className={cn("max-w-full font-mono font-normal", taken && "text-muted-foreground")}
                  onClick={() => onPickModel?.(model)}
                >
                  {taken ? <CircleCheckIcon /> : <PlusIcon />}
                  <span className="truncate">{model}</span>
                </Button>
              )
            })}
            {visible.length === 0 ? <span className="text-xs text-muted-foreground">没有匹配的模型名</span> : null}
          </div>
          {!filter && models.length > PREVIEW ? (
            <Button type="button" variant="link" size="xs" className="self-start px-0" onClick={() => setExpanded((v) => !v)}>
              {expanded ? "收起" : `显示全部 ${models.length} 个`}
            </Button>
          ) : null}
        </div>
      ) : null}
    </Notice>
  )
}
