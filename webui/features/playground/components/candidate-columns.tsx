"use client"

import type { ColumnDef } from "@tanstack/react-table"
import Link from "next/link"

import { ScoreBar } from "@/components/score-bar"
import { ToneBadge } from "@/components/tone-badge"
import type { PlaygroundCandidate } from "@/lib/api/types"
import { formatProb } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { formatRerank, TIER } from "./data"

function MemoryCell({ c }: { c: PlaygroundCandidate }) {
  const severity = labelOf.severity(c.severity)
  return (
    <div className="flex min-w-0 max-w-sm flex-col gap-1" data-injected={c.injected}>
      <Link href={`/enzymes?id=${encodeURIComponent(c.id)}`} onClick={(event) => event.stopPropagation()} className="line-clamp-2 text-sm font-medium break-words hover:underline" title={c.then}>
        {c.then || c.id}
      </Link>
      <div className="flex min-w-0 items-center gap-1.5">
        {c.severity && c.severity !== "info" ? <ToneBadge tone={severity.tone} label={severity.label} /> : null}
        {c.domain ? <span className="shrink-0 text-xs text-muted-foreground">{c.domain}</span> : null}
        <span className="min-w-0 truncate font-mono text-xs text-muted-foreground" title={c.patterns}>
          {c.patterns}
        </span>
      </div>
    </div>
  )
}

function RecallCell({ c }: { c: PlaygroundCandidate }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <span className="text-sm tabular-nums">{c.recall_rank === null ? "—" : `#${c.recall_rank}`}</span>
      <div className="flex flex-wrap gap-1">
        {c.lexical ? <ToneBadge tone="secondary" label="词面" title="词面(BM25)召回命中" /> : null}
        {c.hint !== null ? <ToneBadge tone="info-light" label={`经验 ${formatProb(c.hint)}`} title="当前情境与这条记忆的过往适用情境(经验样本)相似度" /> : null}
        {c.condition_killed ? <ToneBadge tone="destructive-light" label="条件淘汰" title="记忆的适用条件不满足,在这一步出局" /> : null}
      </div>
    </div>
  )
}

function RerankCell({ c }: { c: PlaygroundCandidate }) {
  return (
    <div className="flex flex-col items-start gap-1">
      <span className="text-sm tabular-nums">{formatRerank(c.rerank)}</span>
      {c.floor_passed === false ? <ToneBadge tone="warning-light" label="低于下限" title="精排分低于相关性下限,被淘汰(判官仍可救回)" /> : null}
    </div>
  )
}

function ScoreCell({ c }: { c: PlaygroundCandidate }) {
  const tier = c.tier ? TIER[c.tier] : null
  return (
    <div className="flex flex-col items-start gap-1">
      <span className="text-sm tabular-nums">{c.score === null ? "—" : c.score.toFixed(3)}</span>
      {tier ? <ToneBadge tone={tier.tone} label={tier.label} /> : null}
    </div>
  )
}

function InjectCell({ c }: { c: PlaygroundCandidate }) {
  if (!c.injected) return <span className="text-sm text-muted-foreground">—</span>
  const reason = labelOf.reason(c.reason)
  return (
    <div className="flex flex-wrap items-center gap-1">
      <ToneBadge tone={reason.tone} label={reason.label} title={reason.hint} />
      {c.exploratory ? <ToneBadge tone="warning-light" label="探索" title="冷启动探索项:置信度低但判官高度认可,hook 豁免置信度过滤" /> : null}
      {c.hook_filtered ? <ToneBadge tone="destructive-light" label="会被 hook 过滤" title="置信度低于 hook 的下限,Claude Code 的 hook 会把它丢掉" /> : null}
    </div>
  )
}

/** 每个候选在各阶段留下的信号;已注入的行整行淡淡染绿。 */
export function createCandidateColumns({ showAnalogy }: { showAnalogy: boolean }): ColumnDef<PlaygroundCandidate, unknown>[] {
  const nullsLast = (value: number | null) => (value === null ? -Infinity : value)

  const columns: ColumnDef<PlaygroundCandidate, unknown>[] = [
    { id: "memory", header: "记忆", enableSorting: false, cell: ({ row }) => <MemoryCell c={row.original} /> },
    { id: "confidence", header: "置信度", accessorFn: (c) => nullsLast(c.confidence), cell: ({ row }) => <ScoreBar value={row.original.confidence} /> },
    { id: "recall", header: "召回", accessorFn: (c) => (c.recall_rank === null ? -Infinity : -c.recall_rank), cell: ({ row }) => <RecallCell c={row.original} /> },
    { id: "rerank", header: "精排", accessorFn: (c) => nullsLast(c.rerank), cell: ({ row }) => <RerankCell c={row.original} /> },
    { id: "score", header: "打分", accessorFn: (c) => nullsLast(c.score), cell: ({ row }) => <ScoreCell c={row.original} /> },
    { id: "p", header: "判官 p", accessorFn: (c) => nullsLast(c.p), cell: ({ row }) => <ScoreBar value={row.original.p} /> },
    {
      id: "q",
      header: "单选 q",
      accessorFn: (c) => nullsLast(c.q),
      cell: ({ row }) => <span className={cn("text-sm tabular-nums", row.original.q === null && "text-muted-foreground")}>{formatProb(row.original.q)}</span>,
    },
  ]

  if (showAnalogy) {
    columns.push({
      id: "analogy",
      header: "类比 p / q",
      accessorFn: (c) => nullsLast(c.analog_q),
      cell: ({ row }) => (
        <span className="text-sm tabular-nums">
          {row.original.analog_p === null && row.original.analog_q === null ? "—" : `${formatProb(row.original.analog_p)} / ${formatProb(row.original.analog_q)}`}
        </span>
      ),
    })
  }

  columns.push({ id: "inject", header: "注入", accessorFn: (c) => (c.injected ? 1 : 0), cell: ({ row }) => <InjectCell c={row.original} /> })
  return columns
}
