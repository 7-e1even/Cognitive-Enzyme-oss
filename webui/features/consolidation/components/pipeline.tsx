"use client"

import { CheckIcon, CircleOffIcon } from "lucide-react"
import type { ReactNode } from "react"

import { Notice } from "@/components/notice"
import { StepRail, StepRailItem } from "@/components/step-rail"
import { ToneBadge } from "@/components/tone-badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"

import { REPLAY_LABELS, RESULT_LABELS, STAGES, type ScheduleDraft, type StageDef, type StageId } from "../lib/consolidation"
import { ResultFigures, type ResultFigure } from "./result-figures"

type DraftChange = <K extends keyof ScheduleDraft>(key: K, value: ScheduleDraft[K]) => void

function stageEnabled(id: StageId, draft: ScheduleDraft) {
  if (id === "evolution") return draft.mutation_enabled || draft.crossover_enabled
  if (id === "dreamglia") return draft.dreamglia_enabled
  if (id === "replay") return draft.replay_enabled
  return true
}

/** 这一阶段在上次结果里留下的数字;一个键都没有说明上次跳过了它 */
function stageFigures(stage: StageDef, result: Record<string, unknown> | null): ResultFigure[] {
  if (!result) return []
  if (stage.id === "replay") {
    const replay = result.replay
    if (!replay || typeof replay !== "object") return []
    return Object.entries(replay as Record<string, unknown>)
      .filter(([key, value]) => !(key === "errors" && value === 0) && !(key === "skipped_empty" && value === 0))
      .map(([key, value]) => ({ key, label: REPLAY_LABELS[key]?.label ?? key, hint: REPLAY_LABELS[key]?.hint, value }))
  }
  return stage.resultKeys
    .filter((key) => key in result)
    .map((key) => ({ key, label: RESULT_LABELS[key]?.label ?? key, hint: RESULT_LABELS[key]?.hint, value: result[key] }))
}

function SwitchRow({
  id,
  title,
  description,
  checked,
  disabled,
  onChange,
}: {
  id: string
  title: ReactNode
  description: ReactNode
  checked: boolean
  disabled?: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4 px-3.5 py-2.5">
      <div className="min-w-0">
        <label htmlFor={id} className="cursor-pointer text-sm font-medium">
          {title}
        </label>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <Switch id={id} size="sm" checked={checked} disabled={disabled} onCheckedChange={onChange} />
    </div>
  )
}

function CountRow({
  id,
  title,
  description,
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  id: string
  title: string
  description: string
  value: number
  min: number
  max: number
  disabled?: boolean
  onChange: (value: number) => void
}) {
  return (
    <div className="flex items-center justify-between gap-4 px-3.5 py-2.5">
      <div className="min-w-0">
        <label htmlFor={id} className="text-sm font-medium">
          {title}
        </label>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        step={1}
        value={value}
        disabled={disabled}
        className="h-8 w-20 tabular-nums"
        onChange={(event) => {
          const next = Number(event.target.value)
          if (Number.isFinite(next)) onChange(Math.min(max, Math.max(min, Math.round(next))))
        }}
      />
    </div>
  )
}

function StageControls({ id, draft, onChange, disabled }: { id: StageId; draft: ScheduleDraft; onChange: DraftChange; disabled: boolean }) {
  const group = "divide-y rounded-lg border bg-muted/30"
  if (id === "evolution") {
    return (
      <div className={group}>
        <SwitchRow id="stage-mutation" title="突变" description="改写成熟记忆的触发条件,试探它还能在哪些情境里起作用" checked={draft.mutation_enabled} disabled={disabled} onChange={(checked) => onChange("mutation_enabled", checked)} />
        <SwitchRow id="stage-crossover" title="交叉" description="把经常一起触发的两条记忆融合成一条新的" checked={draft.crossover_enabled} disabled={disabled} onChange={(checked) => onChange("crossover_enabled", checked)} />
      </div>
    )
  }
  if (id === "dreamglia") {
    return (
      <div className={group}>
        <SwitchRow id="stage-dreamglia" title="启用 DreamGlia" description="关闭后梦境演化与免疫巡逻都不再运行" checked={draft.dreamglia_enabled} disabled={disabled} onChange={(checked) => onChange("dreamglia_enabled", checked)} />
        <CountRow
          id="stage-dreams"
          title="每次最多梦境数"
          description="一次巩固最多生成几条新假设(1–10)"
          value={draft.dreamglia_max_dreams}
          min={1}
          max={10}
          disabled={disabled || !draft.dreamglia_enabled}
          onChange={(value) => onChange("dreamglia_max_dreams", value)}
        />
      </div>
    )
  }
  if (id === "replay") {
    return (
      <div className={group}>
        <SwitchRow id="stage-replay" title="启用情景回放" description="用历史情景离线验证低反馈与探索中的记忆" checked={draft.replay_enabled} disabled={disabled} onChange={(checked) => onChange("replay_enabled", checked)} />
        <CountRow
          id="stage-episodes"
          title="每次最多回放情景数"
          description="一次巩固最多考几幕情景(1–20);越多越准,也越费 LLM 调用"
          value={draft.replay_max_episodes}
          min={1}
          max={20}
          disabled={disabled || !draft.replay_enabled}
          onChange={(value) => onChange("replay_max_episodes", value)}
        />
      </div>
    )
  }
  return null
}

/** 没有对话模型时,这一阶段实际会怎样 */
function llmNote(id: StageId, draft: ScheduleDraft): string | null {
  if (id === "evolution" && stageEnabled(id, draft)) return "还没有配置对话模型:突变与交叉会退回规则改写,效果有限。"
  if (id === "dreamglia" && draft.dreamglia_enabled) return "还没有配置对话模型:梦境演化会跳过,免疫巡逻照常进行。"
  if (id === "replay" && draft.replay_enabled) return "还没有配置对话模型:情景回放会跳过。"
  return null
}

/** 巩固流水线:每个阶段讲清楚做什么,开关与参数放在它旁边,上次运行在这一步留下的数字列在最下面。 */
export function Pipeline({
  draft,
  onChange,
  result,
  llmReady,
  disabled,
}: {
  draft: ScheduleDraft
  onChange: DraftChange
  result: Record<string, unknown> | null
  llmReady: boolean | undefined
  disabled: boolean
}) {
  const enabledCount = STAGES.filter((stage) => stageEnabled(stage.id, draft)).length

  return (
    <Card>
      <CardHeader>
        <CardTitle>巩固流水线</CardTitle>
        <CardDescription>
          每次巩固按顺序执行,关掉的阶段直接跳过 · 当前 {enabledCount} / {STAGES.length} 个阶段开启
        </CardDescription>
      </CardHeader>
      <CardContent>
        <StepRail>
          {STAGES.map((stage, index) => {
            const enabled = stageEnabled(stage.id, draft)
            const always = stage.id === "maintenance" || stage.id === "rules"
            const figures = stageFigures(stage, result)
            const note = llmReady === false ? llmNote(stage.id, draft) : null
            return (
              <StepRailItem
                key={stage.id}
                marker={enabled ? <CheckIcon aria-hidden="true" /> : <CircleOffIcon aria-hidden="true" />}
                markerClassName={cn(enabled ? "border-primary bg-primary text-primary-foreground" : "bg-muted")}
                className="pb-8"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold">
                    <span className="mr-1.5 font-normal text-muted-foreground tabular-nums">{index + 1}.</span>
                    {stage.title}
                  </span>
                  {stage.badges?.map((badge) => (
                    <ToneBadge key={badge} tone={badge === "实验性" ? "warning-light" : "info-light"} label={badge} />
                  ))}
                  <ToneBadge tone={always ? "secondary" : enabled ? "success-light" : "outline"} label={always ? "每次执行" : enabled ? "已开启" : "已关闭"} className="ml-auto" />
                </div>
                <div className="mt-2 flex flex-col gap-3">
                  <p className={cn("max-w-[72ch] text-sm leading-relaxed", !enabled && "text-muted-foreground")}>{stage.description}</p>
                  <StageControls id={stage.id} draft={draft} onChange={onChange} disabled={disabled} />
                  {note ? (
                    <Notice tone="warning" title={note} />
                  ) : null}
                  {result ? (
                    <div className="flex flex-col gap-1.5">
                      <span className="text-xs font-medium text-muted-foreground">上次运行</span>
                      {figures.length ? <ResultFigures figures={figures} /> : <span className="text-xs text-muted-foreground">上次没有执行这一阶段。</span>}
                    </div>
                  ) : null}
                </div>
              </StepRailItem>
            )
          })}
        </StepRail>
      </CardContent>
    </Card>
  )
}
