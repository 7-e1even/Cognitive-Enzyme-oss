"use client"

import { MessageCircleQuestionIcon } from "lucide-react"
import type { ReactNode } from "react"

import { EmptyState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { choiceAnswer, noulAnswer, ruleSummary, type ChoiceQuestion, type NoulQuestion, type Question } from "../lib/data"

// 判官题目与各方作答并排:第一列是线上判官(这次决定用的就是它的概率),其后每位老师一列。
// 老师与判官相差 0.3 以上的格子标出来——分歧大的题正是训练判官最该看的样本。

export interface AnswerSource {
  key: string
  name: string
  /** 线上判官(决定依据)还是事后的老师 */
  role: "judge" | "teacher"
  answers: unknown
}

const DISAGREE = 0.3

function ProbCell({ value, judge, strong }: { value: number | null; judge: number | null; strong?: boolean }) {
  if (value === null) return <span className="text-xs text-muted-foreground">—</span>
  const gap = judge === null ? 0 : Math.abs(value - judge)
  const disagree = judge !== null && gap >= DISAGREE
  return (
    <span
      className={cn("inline-flex items-center justify-end gap-1.5 rounded-md px-1.5 py-0.5", disagree && "bg-amber-500/15 ring-1 ring-amber-500/30")}
      title={disagree ? `与判官相差 ${gap.toFixed(2)}` : undefined}
    >
      <span className="h-1 w-10 overflow-hidden rounded-full bg-muted" aria-hidden="true">
        <span className="block h-full rounded-full bg-primary/70" style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </span>
      <span className={cn("text-xs tabular-nums", strong && "font-semibold")}>{value.toFixed(2)}</span>
    </span>
  )
}

function SourceHead({ source }: { source: AnswerSource }) {
  return (
    <TableHead className="text-right text-xs whitespace-nowrap">
      <span className="inline-flex flex-col items-end leading-tight">
        <span className="max-w-32 truncate font-mono" title={source.name}>
          {source.name}
        </span>
        <span className="text-[10px] font-normal text-muted-foreground">{source.role === "judge" ? "线上判官" : "老师"}</span>
      </span>
    </TableHead>
  )
}

function Prompt({ question, truthy, falsy }: { question: string; truthy?: string; falsy?: string }) {
  if (!question && !truthy && !falsy) return null
  return (
    <div className="flex flex-col gap-1 rounded-lg border bg-muted/40 px-3 py-2 text-xs leading-relaxed">
      {question ? <p>{question}</p> : null}
      {truthy ? (
        <p className="text-muted-foreground">
          <span className="font-medium text-emerald-600 dark:text-emerald-400">判真</span>:{truthy}
        </p>
      ) : null}
      {falsy ? (
        <p className="text-muted-foreground">
          <span className="font-medium text-destructive">判假</span>:{falsy}
        </p>
      ) : null}
    </div>
  )
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5">
      <h4 className="flex items-center gap-2 text-sm font-semibold">
        {title}
        {count !== undefined ? <span className="text-xs font-normal text-muted-foreground tabular-nums">{count} 题</span> : null}
      </h4>
      {children}
    </section>
  )
}

function NoulTable({ questions, sources, injectedReason }: { questions: NoulQuestion[]; sources: AnswerSource[]; injectedReason: Record<string, string> }) {
  const judge = sources.find((s) => s.role === "judge")
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader className="bg-muted/40">
          <TableRow>
            <TableHead className="w-12 text-xs">题号</TableHead>
            <TableHead className="text-xs">{questions[0]?.past !== undefined ? "过往情境" : "候选记忆"}</TableHead>
            {sources.map((source) => (
              <SourceHead key={source.key} source={source} />
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {questions.map((q) => {
            const { head, body } = ruleSummary(q.rule)
            const reason = injectedReason[q.key]
            const judgeValue = judge ? noulAnswer(judge.answers, q.key) : null
            return (
              <TableRow key={q.key} className={cn(reason && "bg-emerald-500/5")}>
                <TableCell className="align-top font-mono text-xs">{q.key}</TableCell>
                <TableCell className="max-w-0 align-top whitespace-normal">
                  {q.past !== undefined ? (
                    <p className="line-clamp-3 text-xs leading-relaxed" title={q.past}>
                      {q.past}
                    </p>
                  ) : (
                    <div className="flex min-w-0 flex-col gap-0.5" title={q.rule ? Object.entries(q.rule).map(([k, v]) => `${k}:${v}`).join("\n") : undefined}>
                      <p className="line-clamp-2 text-xs leading-relaxed">{body || head || "—"}</p>
                      {body && head ? <p className="truncate font-mono text-[11px] text-muted-foreground">{head}</p> : null}
                      {reason ? (
                        <span className="mt-0.5">
                          <ToneBadge tone={labelOf.reason(reason).tone} label={`已注入 · ${labelOf.reason(reason).label}`} />
                        </span>
                      ) : null}
                    </div>
                  )}
                </TableCell>
                {sources.map((source) => (
                  <TableCell key={source.key} className="text-right align-top">
                    <ProbCell value={noulAnswer(source.answers, q.key)} judge={source.role === "judge" ? null : judgeValue} strong={source.role === "judge"} />
                  </TableCell>
                ))}
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function ChoiceTable({ question, sources }: { question: ChoiceQuestion; sources: AnswerSource[] }) {
  const judge = sources.find((s) => s.role === "judge")
  const judgeProbs = judge ? choiceAnswer(judge.answers, question.key) : null
  const top = Object.fromEntries(
    sources.map((source) => {
      const probs = choiceAnswer(source.answers, question.key)
      const best = probs ? Object.entries(probs).sort((a, b) => b[1] - a[1])[0]?.[0] : undefined
      return [source.key, best]
    }),
  )
  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table>
        <TableHeader className="bg-muted/40">
          <TableRow>
            <TableHead className="w-14 text-xs">选项</TableHead>
            <TableHead className="text-xs">内容</TableHead>
            {sources.map((source) => (
              <SourceHead key={source.key} source={source} />
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {question.options.map((option) => (
            <TableRow key={option.key} className={cn(option.key === "none" && "bg-muted/30")}>
              <TableCell className="align-top font-mono text-xs">{option.key}</TableCell>
              <TableCell className="max-w-0 align-top whitespace-normal">
                <p className="line-clamp-2 text-xs leading-relaxed" title={option.text}>
                  {option.key === "none" ? <span className="text-muted-foreground">{option.text || "都不是"}</span> : option.text}
                </p>
              </TableCell>
              {sources.map((source) => {
                const probs = choiceAnswer(source.answers, question.key)
                return (
                  <TableCell key={source.key} className="text-right align-top">
                    <ProbCell value={probs ? (probs[option.key] ?? 0) : null} judge={source.role === "judge" || !judgeProbs ? null : (judgeProbs[option.key] ?? 0)} strong={top[source.key] === option.key} />
                  </TableCell>
                )
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

export function DecisionQuestions({
  questions,
  sources,
  injectedReason,
}: {
  questions: Question[]
  sources: AnswerSource[]
  /** 题号(c0…)→ 注入原因;没注入的不在表里 */
  injectedReason: Record<string, string>
}) {
  if (questions.length === 0) {
    return (
      <EmptyState
        icon={MessageCircleQuestionIcon}
        title="这次判定没有判官题目"
        description="判官没有作答(未配置、超时或调用失败),这一次按打分与固定剂量注入。这样的记录仍保留情境与注入结果,但不能用来训练判官。"
      />
    )
  }

  const candidateNoul = questions.filter((q): q is NoulQuestion => q.kind === "noul" && q.past === undefined)
  const analogyNoul = questions.filter((q): q is NoulQuestion => q.kind === "noul" && q.past !== undefined)
  const choices = questions.filter((q): q is ChoiceQuestion => q.kind === "choice")
  const best = choices.find((q) => q.key === "best")
  const same = choices.find((q) => q.key === "same")
  const otherChoices = choices.filter((q) => q !== best && q !== same)

  return (
    <div className="flex flex-col gap-6">
      {sources.length > 1 ? (
        <p className="text-xs leading-relaxed text-muted-foreground">
          每列是一方对同一道题的作答;老师与线上判官相差 {DISAGREE} 以上的格子用黄色标出。加粗的是判官的概率,或单选题里该列最高的选项。
        </p>
      ) : null}
      {candidateNoul.length > 0 ? (
        <Section title="逐条判断 · 这条记忆对这个请求直接有用吗" count={candidateNoul.length}>
          <Prompt question={candidateNoul[0].question} truthy={candidateNoul[0].truthy} falsy={candidateNoul[0].falsy} />
          <NoulTable questions={candidateNoul} sources={sources} injectedReason={injectedReason} />
        </Section>
      ) : null}
      {best ? (
        <Section title="单选 · 哪一条最有用(或都没用)">
          <Prompt question={best.question} />
          <ChoiceTable question={best} sources={sources} />
        </Section>
      ) : null}
      {analogyNoul.length > 0 ? (
        <Section title="类比识别 · 与过往适用情境是同一类情况吗" count={analogyNoul.length}>
          <Prompt question={analogyNoul[0].question} truthy={analogyNoul[0].truthy} falsy={analogyNoul[0].falsy} />
          <NoulTable questions={analogyNoul} sources={sources} injectedReason={{}} />
        </Section>
      ) : null}
      {same ? (
        <Section title="类比单选 · 哪一条过往情境是同一类">
          <Prompt question={same.question} />
          <ChoiceTable question={same} sources={sources} />
        </Section>
      ) : null}
      {otherChoices.map((q) => (
        <Section key={q.key} title={`单选 · ${q.key}`}>
          <Prompt question={q.question} />
          <ChoiceTable question={q} sources={sources} />
        </Section>
      ))}
    </div>
  )
}
