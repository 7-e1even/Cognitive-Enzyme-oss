"use client"

import { useQuery } from "@tanstack/react-query"
import { ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ClockIcon, GraduationCapIcon, ScaleIcon } from "lucide-react"
import Link from "next/link"

import { CopyButton } from "@/components/copy-button"
import { DetailRow, DetailSection } from "@/components/detail-row"
import { JsonView } from "@/components/json-view"
import { RelativeTime } from "@/components/relative-time"
import { EmptyState, ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { adminApi } from "@/lib/api/endpoints"
import type { DecisionCandidate, DecisionDetail } from "@/lib/api/types"
import { formatDateTime, formatNumber, formatProb, shortId } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { ANALOGY_LABEL, JUDGE_STATUS, judgeLabel, num, outcomeLabel, parseQuestions, PROMPT_LABEL, STAGE_LABEL, str, THRESHOLD_LABEL, TIER_LABEL } from "../lib/data"
import { DecisionQuestions, type AnswerSource } from "./decision-questions"

const TABS = [
  { value: "overview", label: "概览" },
  { value: "questions", label: "判官问答" },
  { value: "candidates", label: "候选" },
  { value: "results", label: "结果与标注" },
  { value: "raw", label: "原始 JSON" },
] as const

function Mono({ children }: { children: React.ReactNode }) {
  return <span className="font-mono text-xs">{children}</span>
}

function EnzymeLink({ id }: { id: string }) {
  return (
    <Link href={`/enzymes?id=${encodeURIComponent(id)}`} className="font-mono text-xs underline-offset-4 transition-colors hover:text-primary hover:underline" title={id}>
      {shortId(id)}
    </Link>
  )
}

function OverviewTab({ rec }: { rec: DecisionDetail }) {
  const policy = rec.policy ?? {}
  const judge = judgeLabel(str(policy.judge))
  const rule = (policy.rule && typeof policy.rule === "object" ? policy.rule : {}) as Record<string, unknown>
  const knownRule = new Set(THRESHOLD_LABEL.map(([key]) => key))
  const thresholds: [string, string, unknown][] = [
    ...THRESHOLD_LABEL.filter(([key]) => key in rule).map(([key, label]) => [key, label, rule[key]] as [string, string, unknown]),
    ...Object.keys(rule).filter((key) => !knownRule.has(key)).map((key) => [key, key, rule[key]] as [string, string, unknown]),
  ]
  const state = rec.request?.state ?? ""

  return (
    <div className="flex flex-col pb-4">
      <DetailSection>情境</DetailSection>
      <div className="px-4 pb-2">
        <div className="group/state relative rounded-lg border bg-muted/40">
          <CopyButton value={state} label="情境" className="absolute top-1.5 right-1.5 opacity-0 transition-opacity group-hover/state:opacity-100" />
          <p className="max-h-64 overflow-y-auto px-3 py-2.5 text-sm leading-relaxed break-words whitespace-pre-wrap">
            {state || <span className="text-muted-foreground">(没有记录情境文本)</span>}
          </p>
        </div>
      </div>

      <DetailSection>判定策略</DetailSection>
      <DetailRow label="判官">
        <ToneBadge tone={judge.tone} label={judge.label} title={judge.hint} />
      </DetailRow>
      <DetailRow label="模型">{str(policy.model) ? <Mono>{str(policy.model)}</Mono> : "—"}</DetailRow>
      <DetailRow label="判官状态">{JUDGE_STATUS[str(policy.jev_status)] ?? (str(policy.jev_status) || "—")}</DetailRow>
      <DetailRow label="问法">{PROMPT_LABEL[str(policy.prompt)] ?? (str(policy.prompt) || "—")}</DetailRow>
      <DetailRow label="判定时机">{STAGE_LABEL[str(policy.stage)] ?? (str(policy.stage) || "—")}</DetailRow>
      <DetailRow label="类比识别">{ANALOGY_LABEL[str(policy.analogy)] ?? (str(policy.analogy) || "—")}</DetailRow>
      <DetailRow label="词面召回">{str(policy.lexical) === "on" ? "开启" : str(policy.lexical) === "off" ? "关闭" : "—"}</DetailRow>
      <DetailRow label="来源">{labelOf.decisionSource(str(policy.source) || "prime").label}</DetailRow>

      {thresholds.length > 0 ? (
        <>
          <DetailSection>选择阈值(当时生效)</DetailSection>
          <dl className="grid grid-cols-1 gap-x-6 gap-y-1.5 px-4 py-1.5 sm:grid-cols-2">
            {thresholds.map(([key, label, value]) => (
              <div key={key} className="flex items-center justify-between gap-3 text-sm" title={`selection.${key}`}>
                <dt className="truncate text-muted-foreground">{label}</dt>
                <dd className="font-medium tabular-nums">{typeof value === "number" ? formatProb(value) : str(value)}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}

      <DetailSection>上下文</DetailSection>
      <DetailRow label="时间">{formatDateTime(rec.created_at)}</DetailRow>
      <DetailRow label="会话">
        {rec.session_id ? (
          <span className="inline-flex items-center gap-1">
            <Mono>{rec.session_id}</Mono>
            <CopyButton value={rec.session_id} label="会话 ID" />
          </span>
        ) : (
          "—"
        )}
      </DetailRow>
      <DetailRow label="项目">{rec.project || "—"}</DetailRow>
      <DetailRow label="领域">{rec.domain || "—"}</DetailRow>
      <DetailRow label="情境指纹">
        <Mono>{rec.context_hash}</Mono>
      </DetailRow>
    </div>
  )
}

function CandidateTable({ candidates, injected }: { candidates: DecisionCandidate[]; injected: Record<string, string> }) {
  if (candidates.length === 0) {
    return <EmptyState icon={ScaleIcon} title="判定池是空的" description="判官没有作答时不记录判定池;这次注入了哪些记忆,见「结果与标注」。" />
  }
  const head = "h-9 text-xs"
  const cell = "py-2 text-xs tabular-nums"
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader className="bg-muted/40">
          <TableRow>
            <TableHead className={head}>题号</TableHead>
            <TableHead className={head}>记忆</TableHead>
            <TableHead className={cn(head, "text-right")} title="向量 + 词面召回的名次">召回</TableHead>
            <TableHead className={cn(head, "text-right")} title="精排分(下限过滤之前)">精排</TableHead>
            <TableHead className={head}>分层</TableHead>
            <TableHead className={head} title="经验样本相似度(召回时认出的过往情境)">经验</TableHead>
            <TableHead className={cn(head, "text-right")} title="逐条判断:对这个请求直接有用的概率">p</TableHead>
            <TableHead className={cn(head, "text-right")} title="单选:是最有用那一条的概率">q</TableHead>
            <TableHead className={cn(head, "text-right")} title="类比识别的逐条 / 单选概率">类比 p / q</TableHead>
            <TableHead className={head}>注入</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {candidates.map((c) => {
            const reason = injected[c.id]
            return (
              <TableRow key={c.key} className={cn(reason && "bg-emerald-500/5")}>
                <TableCell className={cn(cell, "font-mono")}>{c.key}</TableCell>
                <TableCell className={cell}>
                  <span className="flex flex-wrap items-center gap-1">
                    <EnzymeLink id={c.id} />
                    {c.version !== null ? <span className="text-muted-foreground">v{c.version}</span> : null}
                    {c.lexical ? <ToneBadge tone="secondary" label="词面" title="词面(BM25)召回命中" /> : null}
                    {c.rescued ? <ToneBadge tone="warning-light" label="救援" title="被精排或下限淘汰,又因判官强烈认可回到判定池" /> : null}
                  </span>
                </TableCell>
                <TableCell className={cn(cell, "text-right")}>{c.recall_rank ?? "—"}</TableCell>
                <TableCell className={cn(cell, "text-right")}>{c.rerank === null ? "—" : c.rerank < 0.001 ? "<0.001" : c.rerank.toFixed(3)}</TableCell>
                <TableCell className={cell}>{c.tier ? (TIER_LABEL[c.tier] ?? c.tier) : "—"}</TableCell>
                <TableCell className={cell}>
                  {c.hint === null ? "—" : formatProb(c.hint)}
                  {c.votes > 0 ? <span className="text-muted-foreground"> · {c.votes} 票</span> : null}
                </TableCell>
                <TableCell className={cn(cell, "text-right font-medium")}>{formatProb(c.p)}</TableCell>
                <TableCell className={cn(cell, "text-right")}>{formatProb(c.q)}</TableCell>
                <TableCell className={cn(cell, "text-right")}>{c.analog_p === null && c.analog_q === null ? "—" : `${formatProb(c.analog_p)} / ${formatProb(c.analog_q)}`}</TableCell>
                <TableCell className={cell}>
                  {reason ? <ToneBadge tone={labelOf.reason(reason).tone} label={labelOf.reason(reason).label} /> : <span className="text-muted-foreground">—</span>}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function ResultsTab({ rec }: { rec: DecisionDetail }) {
  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-2.5">
        <h4 className="text-sm font-semibold">注入({rec.injected.length})</h4>
        {rec.injected.length === 0 ? (
          <p className="text-sm text-muted-foreground">这一次保持静默,没有推送任何记忆。</p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {rec.injected.map((item) => {
              const reason = labelOf.reason(item.reason)
              return (
                <li key={item.id} className="flex items-center justify-between gap-3 px-3 py-2">
                  <EnzymeLink id={item.id} />
                  <span className="flex items-center gap-1.5">
                    {item.exploratory ? <ToneBadge tone="warning-light" label="探索" title="冷启动解锁的探索项:置信度低但判官高度认可,hook 豁免置信度过滤" /> : null}
                    <ToneBadge tone={reason.tone} label={reason.label} title={reason.hint} />
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-2.5">
        <h4 className="text-sm font-semibold">事后结果({rec.outcomes.length})</h4>
        {rec.outcomes.length === 0 ? (
          <p className="text-sm text-muted-foreground">还没有回填结果。智能体调用 report、会话捕获做被动评估之后,结果会挂到这条判定上。</p>
        ) : (
          <ol className="flex flex-col gap-3 border-l pl-4">
            {rec.outcomes.map((outcome, index) => {
              const label = outcomeLabel(outcome.outcome)
              return (
                <li key={`${outcome.enzyme_id}-${index}`} className="flex flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <ToneBadge tone={label.tone} label={label.label} title={label.hint} />
                    <EnzymeLink id={outcome.enzyme_id} />
                    {outcome.source ? <span className="text-xs text-muted-foreground">来自 {outcome.source}</span> : null}
                  </div>
                  <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                    <ClockIcon className="size-3" aria-hidden="true" />
                    <RelativeTime value={outcome.created_at} />
                  </span>
                </li>
              )
            })}
          </ol>
        )}
      </section>

      <section className="flex flex-col gap-2.5">
        <h4 className="text-sm font-semibold">老师标注({rec.teacher_labels.length})</h4>
        {rec.teacher_labels.length === 0 ? (
          <p className="text-sm text-muted-foreground">还没有老师为这条判定作答。配置影子判官,或离线运行 python -m app.distill.label。</p>
        ) : (
          <div className="flex flex-col gap-2">
            {rec.teacher_labels.map((label) => {
              const usage = label.usage ?? {}
              const tokens = num(usage.total_tokens) ?? (num(usage.prompt_tokens) ?? 0) + (num(usage.completion_tokens) ?? 0)
              return (
                <Collapsible key={label.teacher} className="group/label rounded-lg border">
                  <CollapsibleTrigger className="flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left transition-colors hover:bg-muted/40">
                    <span className="flex min-w-0 items-center gap-2">
                      <GraduationCapIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                      <span className="truncate font-mono text-sm">{label.teacher}</span>
                    </span>
                    <span className="flex shrink-0 items-center gap-3 text-xs text-muted-foreground">
                      {tokens ? <span className="tabular-nums">{formatNumber(tokens)} tokens</span> : null}
                      <RelativeTime value={label.created_at} />
                      <ChevronDownIcon className="size-4 transition-transform group-data-[state=open]/label:rotate-180" aria-hidden="true" />
                    </span>
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <div className="border-t p-3">
                      <JsonView value={label.answers} />
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              )
            })}
          </div>
        )}
      </section>
    </div>
  )
}

/** 一次 prime 判定的完整记录。判定记录是历史事实,没有可编辑的字段。底部是上一条 / 下一条,方便顺着列表逐条审阅。 */
export function DecisionSheet({
  decisionId,
  open,
  onOpenChange,
  onPrev,
  onNext,
}: {
  decisionId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 列表里的上一条 / 下一条;在本页边界时为空 */
  onPrev?: () => void
  onNext?: () => void
}) {
  const query = useQuery({
    queryKey: ["admin", "decision", decisionId],
    queryFn: () => adminApi.decision(decisionId),
    enabled: Boolean(decisionId),
  })
  const rec = query.data
  const policy = rec?.policy ?? {}
  const judge = judgeLabel(str(policy.judge))
  const source = labelOf.decisionSource(str(policy.source) || "prime")
  const questions = parseQuestions(rec?.request?.questions)
  const injectedById = Object.fromEntries((rec?.injected ?? []).map((item) => [item.id, item.reason]))
  const injectedByKey = Object.fromEntries((rec?.candidates ?? []).filter((c) => injectedById[c.id]).map((c) => [c.key, injectedById[c.id]]))
  const sources: AnswerSource[] = rec
    ? [
        { key: "judge", name: str(policy.model) || judge.label, role: "judge", answers: rec.answers },
        ...rec.teacher_labels.map((t) => ({ key: `t:${t.teacher}`, name: t.teacher, role: "teacher" as const, answers: t.answers })),
      ]
    : []

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-4xl">
        <SheetHeader className="gap-2.5 border-b pr-12">
          <SheetTitle className="flex flex-wrap items-center gap-2">
            <ScaleIcon className="size-4 text-muted-foreground" aria-hidden="true" />
            <span className="font-mono text-base">{shortId(decisionId, 12)}</span>
            <CopyButton value={decisionId} label="判定 ID" />
            {rec ? (
              <>
                <ToneBadge tone={source.tone} label={source.label} />
                <ToneBadge tone={judge.tone} label={judge.label} title={judge.hint} />
              </>
            ) : null}
          </SheetTitle>
          <SheetDescription className="text-xs">
            {rec
              ? `${formatDateTime(rec.created_at)} · 候选 ${rec.candidates.length} · 注入 ${rec.injected.length} · 结果 ${rec.outcomes.length} · 标注 ${rec.teacher_labels.length}${str(policy.model) ? ` · ${str(policy.model)}` : ""}`
              : "一次 prime 判定的完整记录:情境、判定策略与阈值、判官题目和各方作答、候选信号、注入、事后结果与老师标注。"}
          </SheetDescription>
        </SheetHeader>

        {query.isLoading ? (
          <div className="flex flex-col gap-3 p-5">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : query.isError ? (
          <div className="p-5">
            <ErrorState error={query.error} onRetry={() => void query.refetch()} title="判定记录加载失败" />
          </div>
        ) : rec ? (
          <Tabs key={decisionId} defaultValue="overview" className="flex min-h-0 flex-1 flex-col gap-0">
            <div className="shrink-0 border-b px-5">
              <TabsList variant="line" className="gap-5">
                {TABS.map((tab) => (
                  <TabsTrigger key={tab.value} value={tab.value} className="gap-1.5 px-0">
                    {tab.label}
                    {tab.value === "questions" && questions.length > 0 ? <span className="text-xs text-muted-foreground tabular-nums">{questions.length}</span> : null}
                    {tab.value === "candidates" && rec.candidates.length > 0 ? <span className="text-xs text-muted-foreground tabular-nums">{rec.candidates.length}</span> : null}
                  </TabsTrigger>
                ))}
              </TabsList>
            </div>
            <div className="min-h-0 flex-1">
              <ScrollArea className="h-full">
                <TabsContent value="overview">
                  <OverviewTab rec={rec} />
                </TabsContent>
                <TabsContent value="questions">
                  <div className="px-5 py-5">
                    <DecisionQuestions questions={questions} sources={sources} injectedReason={injectedByKey} />
                  </div>
                </TabsContent>
                <TabsContent value="candidates">
                  <div className="flex flex-col gap-3 px-5 py-5">
                    <p className="text-xs leading-relaxed text-muted-foreground">判定池里每条记忆当时的上游信号与判官概率;题号 c0、c1… 对应「判官问答」里的逐条判断。</p>
                    <CandidateTable candidates={rec.candidates} injected={injectedById} />
                  </div>
                </TabsContent>
                <TabsContent value="results">
                  <div className="px-5 py-5">
                    <ResultsTab rec={rec} />
                  </div>
                </TabsContent>
                <TabsContent value="raw">
                  <div className="px-5 py-5">
                    <JsonView value={rec} className="max-h-none" />
                  </div>
                </TabsContent>
              </ScrollArea>
            </div>
          </Tabs>
        ) : null}

        <SheetFooter className="flex-row items-center justify-between border-t">
          <div className="flex items-center gap-1">
            <Button type="button" variant="outline" size="sm" disabled={!onPrev} onClick={onPrev}>
              <ChevronLeftIcon /> 上一条
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={!onNext} onClick={onNext}>
              下一条 <ChevronRightIcon />
            </Button>
          </div>
          <Button type="button" variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            关闭
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}
