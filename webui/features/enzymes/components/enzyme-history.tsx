"use client"

import { ChevronDownIcon } from "lucide-react"
import { useState } from "react"

import { JsonView } from "@/components/json-view"
import { RelativeTime } from "@/components/relative-time"
import { StepRail, StepRailItem } from "@/components/step-rail"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { EvolutionEvent } from "@/lib/api/types"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { summarizeEvent, type QualityAssessment } from "../lib/events"
import { QUALITY_DIMENSIONS, QUALITY_GATE } from "../lib/options"

const EXEMPLARS_SHOWN = 3

function ExemplarItem({ text, anti }: { text: string; anti?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const long = text.length > 160
  return (
    <li className={cn("rounded-lg border bg-muted/40 px-3 py-2.5", anti && "border-destructive/20 bg-destructive/5")}>
      <p className={cn("text-sm leading-relaxed break-words whitespace-pre-wrap", long && !expanded && "line-clamp-3")}>{text}</p>
      {long ? (
        <button type="button" className="mt-1 text-xs text-primary hover:underline" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "收起" : "展开全文"}
        </button>
      ) : null}
    </li>
  )
}

/** 经验样本:这条记忆被证实适用(或不适用)过的真实情境,召回时靠它们做类比识别。 */
export function ExemplarList({ items, anti = false }: { items: string[]; anti?: boolean }) {
  const [showAll, setShowAll] = useState(false)
  const visible = showAll ? items : items.slice(0, EXEMPLARS_SHOWN)
  return (
    <div className="flex flex-col gap-2 px-4 pb-2">
      <ul className="flex flex-col gap-2">
        {visible.map((text, index) => (
          <ExemplarItem key={`${index}-${text.slice(0, 24)}`} text={text} anti={anti} />
        ))}
      </ul>
      {items.length > EXEMPLARS_SHOWN ? (
        <Button type="button" variant="ghost" size="xs" className="w-fit" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "收起" : `显示全部 ${items.length} 条`}
        </Button>
      ) : null}
    </div>
  )
}

/** 质量门的五维评分(1~5)。 */
export function QualityBreakdown({ quality }: { quality: QualityAssessment }) {
  const tone = quality.score >= QUALITY_GATE.pass ? "bg-emerald-500" : quality.score >= QUALITY_GATE.refine ? "bg-primary/70" : "bg-amber-500"
  return (
    <div className="flex flex-col gap-3 px-4 pb-2">
      <div className="flex items-baseline gap-2">
        <span className="text-2xl font-semibold tabular-nums">{quality.score.toFixed(1)}</span>
        <span className="text-sm text-muted-foreground">/ 5 分</span>
        <span className="ml-auto text-xs text-muted-foreground">
          自动通过线 {QUALITY_GATE.pass} · 隔离线 {QUALITY_GATE.quarantine}
        </span>
      </div>
      <dl className="grid gap-2">
        {QUALITY_DIMENSIONS.map((dimension) => {
          const value = quality.dimensions[dimension.key]
          return (
            <div key={dimension.key} className="grid grid-cols-[4.5rem_minmax(0,1fr)_2rem] items-center gap-3 text-sm">
              <Tooltip>
                <TooltipTrigger asChild>
                  <dt className="w-fit cursor-help text-muted-foreground">{dimension.label}</dt>
                </TooltipTrigger>
                <TooltipContent>{dimension.hint}</TooltipContent>
              </Tooltip>
              <dd className="h-1.5 overflow-hidden rounded-full bg-muted">
                {value !== undefined ? <span className={cn("block h-full rounded-full", tone)} style={{ width: `${(value / 5) * 100}%` }} /> : null}
              </dd>
              <dd className="text-right text-xs tabular-nums">{value !== undefined ? value.toFixed(1) : "—"}</dd>
            </div>
          )
        })}
      </dl>
      {quality.insight && quality.finalInsight && quality.insight !== quality.finalInsight ? (
        <p className="text-xs leading-relaxed text-muted-foreground">
          原始表述:<span className="text-foreground">{quality.insight}</span>
        </p>
      ) : null}
    </div>
  )
}

function EventRow({ event }: { event: EvolutionEvent }) {
  const meta = labelOf.event(event.event_type)
  const summary = summarizeEvent(event)
  return (
    <StepRailItem marker={<span className="size-1.5 rounded-full bg-primary/60" />} className="pb-4">
      <div className="flex flex-wrap items-center gap-2">
        <ToneBadge tone={meta.tone} label={meta.label} />
        <RelativeTime value={event.created_at} className="text-xs text-muted-foreground" />
      </div>
      {summary ? <p className="mt-1 text-sm leading-relaxed break-words">{summary}</p> : null}
      <Collapsible>
        <CollapsibleTrigger className="group/raw mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
          <ChevronDownIcon className="size-3 transition-transform group-data-[state=open]/raw:rotate-180" aria-hidden="true" />
          原始数据
        </CollapsibleTrigger>
        <CollapsibleContent>
          <JsonView value={event.detail} className="mt-1.5" />
        </CollapsibleContent>
      </Collapsible>
    </StepRailItem>
  )
}

const EVENTS_SHOWN = 6

export function EvolutionTimeline({ events, loading, limit }: { events: EvolutionEvent[] | undefined; loading: boolean; limit: number }) {
  const [showAll, setShowAll] = useState(false)
  if (loading) {
    return (
      <div className="flex flex-col gap-3 px-4 pb-4">
        {Array.from({ length: 3 }).map((_, index) => (
          <Skeleton key={index} className="h-10 w-full" />
        ))}
      </div>
    )
  }
  if (!events?.length) return <p className="px-4 pb-4 text-sm text-muted-foreground">还没有进化记录。反馈、巩固与免疫巡检都会在这里留下痕迹。</p>
  const visible = showAll ? events : events.slice(0, EVENTS_SHOWN)
  return (
    <div className="flex flex-col gap-3 px-4 pb-4">
      <StepRail>
        {visible.map((event, index) => (
          <EventRow key={event.id ?? `${event.event_type}-${index}`} event={event} />
        ))}
      </StepRail>
      {events.length > EVENTS_SHOWN ? (
        <Button type="button" variant="ghost" size="xs" className="w-fit" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "收起" : `显示全部 ${events.length} 条`}
        </Button>
      ) : null}
      {showAll && events.length >= limit ? <p className="text-xs text-muted-foreground">只显示最近 {limit} 条;更早的记录在「进化事件」页查看。</p> : null}
    </div>
  )
}
