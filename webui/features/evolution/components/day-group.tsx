"use client"

import { ArrowRightIcon, ChevronDownIcon } from "lucide-react"
import Link from "next/link"
import { useState } from "react"

import { JsonView } from "@/components/json-view"
import { StepRail, StepRailItem } from "@/components/step-rail"
import { ToneBadge } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { EvolutionEvent } from "@/lib/api/types"
import { formatDateTime, formatProb, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { clockTime, EVENT_ICONS, summarizeEvent, type Tone } from "../lib/events"

/** 一天里先显示这么多条,多的折起来——反馈学习一天能有上百条 */
const DAY_PREVIEW = 20

const TONE_MARKER: Record<Tone, string> = {
  positive: "border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  negative: "border-destructive/40 bg-destructive/10 text-destructive",
  neutral: "bg-muted text-muted-foreground",
}

function EnzymeLink({ id, label, names }: { id: string; label?: string; names: Map<string, string> }) {
  const name = names.get(id)
  return (
    <Link
      href={`/enzymes?id=${encodeURIComponent(id)}`}
      className="group/enzyme inline-flex max-w-full min-w-0 items-center gap-1 transition-colors hover:text-primary"
      title={name ? `${name}(${id})` : id}
    >
      {label ? <span className="shrink-0 text-muted-foreground">{label}</span> : null}
      {name ? <span className="truncate">{name}</span> : <span className="font-mono">{shortId(id)}</span>}
      <ArrowRightIcon className="size-3 shrink-0 opacity-0 transition-opacity group-hover/enzyme:opacity-100" aria-hidden="true" />
    </Link>
  )
}

function ChangeChip({ label, from, to }: { label: string; from: number; to: number }) {
  const up = to > from
  const flat = Math.abs(to - from) < 1e-6
  return (
    <Badge variant="outline" className="gap-1 font-normal tabular-nums">
      <span className="text-muted-foreground">{label}</span>
      {formatProb(from)}
      <span className="text-muted-foreground">→</span>
      <span className={cn("font-medium", !flat && (up ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"))}>{formatProb(to)}</span>
    </Badge>
  )
}

function EventRow({ event, names }: { event: EvolutionEvent; names: Map<string, string> }) {
  const summary = summarizeEvent(event)
  const type = labelOf.event(event.event_type)
  const Icon = EVENT_ICONS[event.event_type]

  return (
    <StepRailItem marker={Icon ? <Icon aria-hidden="true" /> : null} markerClassName={TONE_MARKER[summary.tone]} className="pb-6">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <ToneBadge tone={type.tone} label={type.label} className="shrink-0" />
        <span className="min-w-0 text-sm leading-5 font-medium break-words">{summary.headline}</span>
      </div>

      <div className="mt-1.5 flex min-w-0 flex-col gap-2">
        {summary.changes.length ? (
          <div className="flex flex-wrap gap-1.5">
            {summary.changes.map((change) => (
              <ChangeChip key={change.label} {...change} />
            ))}
          </div>
        ) : null}

        {summary.quote ? <p className="line-clamp-2 max-w-[80ch] border-l-2 pl-2.5 text-xs leading-relaxed text-muted-foreground">{summary.quote}</p> : null}

        <Collapsible className="flex min-w-0 flex-col gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Tooltip>
              <TooltipTrigger asChild>
                <time dateTime={event.created_at ?? undefined} className="tabular-nums">
                  {clockTime(event.created_at)}
                </time>
              </TooltipTrigger>
              <TooltipContent>{formatDateTime(event.created_at)}</TooltipContent>
            </Tooltip>
            {event.enzyme_id ? (
              <span className="max-w-[28rem] min-w-0">
                <EnzymeLink id={event.enzyme_id} names={names} />
              </span>
            ) : null}
            {summary.related.map((item) => (
              <span key={`${item.label}-${item.id}`} className="max-w-[20rem] min-w-0">
                <EnzymeLink id={item.id} label={item.label} names={names} />
              </span>
            ))}
            {summary.tags.map((tag) => (
              <span key={tag} className="rounded-md bg-muted px-1.5 py-0.5">
                {tag}
              </span>
            ))}
            <CollapsibleTrigger className="group/detail inline-flex items-center gap-0.5 transition-colors hover:text-foreground">
              明细
              <ChevronDownIcon className="size-3.5 transition-transform group-data-[state=open]/detail:rotate-180" aria-hidden="true" />
            </CollapsibleTrigger>
          </div>
          <CollapsibleContent>
            <JsonView value={event.detail} className="max-w-3xl" />
          </CollapsibleContent>
        </Collapsible>
      </div>
    </StepRailItem>
  )
}

/** 一天的事件:日期抬头 + 竖向时间线;超过 20 条先折起来 */
export function DayGroup({ title, subtitle, events, names }: { title: string; subtitle: string; events: EvolutionEvent[]; names: Map<string, string> }) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? events : events.slice(0, DAY_PREVIEW)
  const hidden = events.length - visible.length

  return (
    <section className="flex flex-col" aria-label={`${title} ${subtitle}`}>
      <header className="flex items-center justify-between gap-3 border-y bg-muted/40 px-4 py-2 first:border-t-0">
        <div className="flex min-w-0 items-baseline gap-2">
          <h3 className="text-sm font-semibold">{title}</h3>
          {subtitle ? <span className="text-xs text-muted-foreground">{subtitle}</span> : null}
        </div>
        <span className="text-xs text-muted-foreground tabular-nums">{events.length} 条</span>
      </header>
      <div className="px-4 py-5">
        <StepRail>
          {visible.map((event, index) => (
            <EventRow key={event.id ?? `${event.created_at}-${index}`} event={event} names={names} />
          ))}
        </StepRail>
        {hidden > 0 ? (
          <Button type="button" variant="ghost" size="sm" className="mt-4 ml-10" onClick={() => setExpanded(true)}>
            <ChevronDownIcon /> 再显示这一天的 {hidden} 条
          </Button>
        ) : null}
      </div>
    </section>
  )
}
