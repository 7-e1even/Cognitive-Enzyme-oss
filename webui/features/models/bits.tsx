import { LinkIcon } from "lucide-react"
import Link from "next/link"

import { ToneBadge, type Tone } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import type { ModelKind, Protocol } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { monogram, usageHref, usageLabel } from "./lib/registry"

/** 服务商的图标块:没有各家品牌图标,里面写名字的缩写。 */
export function ProviderLogo({ name, size = "default" }: { name: string; size?: "sm" | "default" }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex shrink-0 items-center justify-center rounded-xl border bg-muted/60 font-semibold tracking-tight text-foreground/80",
        size === "sm" ? "size-8 text-[11px]" : "size-10 text-xs",
      )}
    >
      {monogram(name)}
    </span>
  )
}

export type ProviderState = "connected" | "failed" | "paused" | "missing_key" | "untested"

const LOCAL_URL = /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:|\/|$)/

export function providerState(
  provider: { enabled: boolean; api_key_set: boolean; base_url: string },
  probe: { ok: boolean } | undefined,
): ProviderState {
  if (!provider.enabled) return "paused"
  if (!provider.api_key_set && !LOCAL_URL.test(provider.base_url)) return "missing_key"
  if (!probe) return "untested"
  return probe.ok ? "connected" : "failed"
}

const STATE_META: Record<ProviderState, { label: string; tone: Tone }> = {
  connected: { label: "已连通", tone: "success-light" },
  failed: { label: "连接失败", tone: "warning-light" },
  paused: { label: "已停用", tone: "outline" },
  missing_key: { label: "缺少密钥", tone: "destructive-light" },
  untested: { label: "已启用", tone: "success-light" },
}

export function ProviderStateBadge({ state }: { state: ProviderState }) {
  const meta = STATE_META[state]
  return <ToneBadge tone={meta.tone} label={meta.label} />
}

export function ProtocolBadge({ protocol }: { protocol: Protocol | "" }) {
  const meta = labelOf.protocol(protocol)
  return <ToneBadge label={meta.label} tone={meta.tone} title={meta.hint} />
}

export function KindBadge({ kind, className }: { kind: ModelKind; className?: string }) {
  const meta = labelOf.kind(kind)
  return <ToneBadge label={meta.label} tone={meta.tone} className={className} />
}

/** 模型被哪些环节引用;点进去就是改这个引用的那一页。 */
export function UsageBadges({ usedBy, className }: { usedBy: string[]; className?: string }) {
  if (usedBy.length === 0) return null
  return (
    <span className={cn("flex flex-wrap items-center gap-1", className)}>
      {usedBy.map((key) => (
        <Badge key={key} variant="outline" asChild>
          <Link href={usageHref(key)} title={`在「${usageLabel(key)}」中使用`} className="hover:bg-muted">
            <LinkIcon aria-hidden="true" />
            {usageLabel(key)}
          </Link>
        </Badge>
      ))}
    </span>
  )
}
