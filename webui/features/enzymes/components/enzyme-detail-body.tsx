"use client"

import { useQueries } from "@tanstack/react-query"
import { ExternalLinkIcon, PencilIcon } from "lucide-react"
import { useState, type ReactNode } from "react"

import { DetailRow, DetailSection } from "@/components/detail-row"
import { Notice } from "@/components/notice"
import { OptionSelect } from "@/components/option-select"
import { ToneBadge } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"
import { Textarea } from "@/components/ui/textarea"
import { enzymeApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { EnzymeDetail, EnzymeStatus, EnzymeUpdate, EvolutionEvent, PatternItem, Severity } from "@/lib/api/types"
import { formatDateTime, formatNumber, formatRelative, shortId } from "@/lib/format"
import { cn } from "@/lib/utils"
import { fieldError } from "@/lib/validation"

import { ENZYME_TYPE_LABEL, latestQuarantineReason, qualityAssessment } from "../lib/events"
import { SEVERITY_OPTIONS, STATUS_OPTIONS, relationLabel } from "../lib/options"
import { patternText } from "../lib/patterns"
import { useUpdateEnzyme } from "../lib/queries"
import { enzymeFieldSchema } from "../lib/schema"
import { ConfidenceMeter, DomainLabel, OriginBadge, QualityScore, SeverityBadge, StatusBadge } from "./enzyme-badges"
import { DomainInput, ListEditor, PatternEditor } from "./editors"
import { EditableRow } from "./editable-row"
import { EvolutionTimeline, ExemplarList, QualityBreakdown } from "./enzyme-history"

type EditableField =
  | "then"
  | "because"
  | "patterns"
  | "domain"
  | "tags"
  | "severity"
  | "status"
  | "confidence"
  | "conditions"
  | "negations"
  | "references"

interface Draft {
  then: string
  because: string
  patterns: PatternItem[]
  domain: string
  tags: string[]
  severity: Severity
  status: EnzymeStatus
  confidence: string
  conditions: string[]
  negations: string[]
  references: string[]
}

function toDraft(detail: EnzymeDetail): Draft {
  return {
    then: detail.suggestion,
    because: detail.reason,
    patterns: detail.pattern_list,
    domain: detail.domain,
    tags: detail.tags,
    severity: detail.severity,
    status: detail.status,
    confidence: String(detail.confidence),
    conditions: detail.conditions,
    negations: detail.negations,
    references: detail.references,
  }
}

function toPayload(field: EditableField, draft: Draft): EnzymeUpdate {
  switch (field) {
    case "then":
      return { then: draft.then.trim() }
    case "because":
      return { because: draft.because.trim() }
    case "domain":
      return { domain: draft.domain.trim() }
    case "confidence":
      return { confidence: Number(draft.confidence) }
    case "patterns":
      return { patterns: draft.patterns }
    case "tags":
      return { tags: draft.tags }
    case "severity":
      return { severity: draft.severity }
    case "status":
      return { status: draft.status }
    case "conditions":
      return { conditions: draft.conditions }
    case "negations":
      return { negations: draft.negations }
    case "references":
      return { references: draft.references }
  }
}

const NotSet = ({ children = "未设置" }: { children?: ReactNode }) => <span className="text-muted-foreground">{children}</span>

function TextValue({ text }: { text: string }) {
  if (!text) return <NotSet />
  return <span className="break-words whitespace-pre-wrap">{text}</span>
}

function ChipsValue({ items }: { items: string[] }) {
  if (!items.length) return <NotSet />
  return (
    <span className="flex min-w-0 flex-wrap gap-1">
      {items.map((item, index) => (
        <Badge key={`${item}-${index}`} variant="outline" className="max-w-full">
          <span className="truncate">{item}</span>
        </Badge>
      ))}
    </span>
  )
}

function PatternsValue({ patterns }: { patterns: PatternItem[] }) {
  if (!patterns.length) return <NotSet />
  return (
    <span className="flex min-w-0 flex-wrap gap-1">
      {patterns.map((item, index) => (
        <ToneBadge
          key={`${item.type}-${item.value}-${index}`}
          tone={item.type === "regex" ? "info-light" : "outline"}
          className="max-w-full font-mono"
        >
          <span className="truncate">{patternText(item)}</span>
        </ToneBadge>
      ))}
    </span>
  )
}

function StatTile({ label, value, tone }: { label: string; value: ReactNode; tone?: "success" | "destructive" }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5 rounded-lg border bg-muted/30 px-3 py-2">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span
        className={cn(
          "text-base font-semibold tabular-nums",
          tone === "success" && "text-emerald-600 dark:text-emerald-400",
          tone === "destructive" && "text-destructive",
        )}
      >
        {value}
      </span>
    </div>
  )
}

function IdLink({ id, onOpen }: { id: string; onOpen: (id: string) => void }) {
  return (
    <button type="button" className="font-mono text-xs text-primary underline-offset-2 hover:underline" onClick={() => onOpen(id)} title={id}>
      {shortId(id)}
    </button>
  )
}

/** 关联记忆:详情接口只给 id,逐条取详情显示内容。 */
function RelatedList({ related, onOpen }: { related: { id: string; relation: string }[]; onOpen: (id: string) => void }) {
  const results = useQueries({
    queries: related.map((item) => ({
      queryKey: qk.enzyme(item.id),
      queryFn: () => enzymeApi.get(item.id),
      staleTime: 60_000,
    })),
  })
  return (
    <ul className="flex flex-col gap-1.5 px-4 pb-2">
      {related.map((item, index) => {
        const detail = results[index]?.data
        return (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => onOpen(item.id)}
              className="flex w-full min-w-0 items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors hover:bg-muted/60"
            >
              <Badge variant="secondary" className="shrink-0">
                {relationLabel(item.relation)}
              </Badge>
              <span className="min-w-0 flex-1 truncate text-sm">{detail?.suggestion ?? (results[index]?.isError ? "已不存在" : shortId(item.id))}</span>
              {detail ? <StatusBadge status={detail.status} /> : null}
            </button>
          </li>
        )
      })}
    </ul>
  )
}

export function EnzymeDetailBody({
  detail,
  events,
  eventsLoading,
  eventsLimit,
  domains,
  onOpenEnzyme,
}: {
  detail: EnzymeDetail
  events: EvolutionEvent[] | undefined
  eventsLoading: boolean
  eventsLimit: number
  domains: Record<string, number>
  onOpenEnzyme: (id: string) => void
}) {
  const update = useUpdateEnzyme()
  const [draft, setDraft] = useState<Draft>(() => toDraft(detail))
  const [editing, setEditing] = useState<EditableField | null>(null)
  const [saving, setSaving] = useState<EditableField | null>(null)
  const [seen, setSeen] = useState(detail)

  // 换了一条记忆,或保存后详情重新取回:从屏幕上的最新值重新开始,不留半截编辑
  if (seen !== detail) {
    setSeen(detail)
    setDraft(toDraft(detail))
    setEditing(null)
  }

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((current) => ({ ...current, [key]: value }))
  const cancelRow = () => {
    setDraft(toDraft(detail))
    setEditing(null)
  }
  const rowError = (field: EditableField) => fieldError(enzymeFieldSchema, field, draft[field])

  /** 保存一行;返回是否已经落库(或本来就没改)。 */
  const saveRow = async (field: EditableField) => {
    if (saving || rowError(field)) return false
    const original = toDraft(detail)[field]
    const next = draft[field]
    const same =
      field === "confidence"
        ? Number(next) === detail.confidence
        : JSON.stringify(typeof next === "string" ? next.trim() : next) === JSON.stringify(original)
    if (same) {
      setEditing(null)
      return true
    }
    setSaving(field)
    try {
      await update.mutateAsync({ id: detail.id, data: toPayload(field, draft) })
      setEditing(null)
      return true
    } catch {
      // 失败原因由接口错误提示给出;行保持编辑状态,方便改完再存
      return false
    } finally {
      setSaving(null)
    }
  }

  const rowProps = (field: EditableField) => ({
    editing: editing === field,
    saving: saving === field,
    actionsDisabled: saving !== null && saving !== field,
    error: editing === field ? rowError(field) : undefined,
    onEdit: () => {
      setDraft(toDraft(detail))
      setEditing(field)
    },
    onCancel: cancelRow,
    onSave: () => void saveRow(field),
  })

  const quality = qualityAssessment(events)
  const quarantine = detail.status === "quarantine" ? latestQuarantineReason(events) : null
  const related = detail.related ?? []

  return (
    <div className="flex flex-col pb-4">
      {detail.status === "quarantine" ? (
        <div className="px-4 pt-4">
          <Notice tone="warning" title="隔离中,不参与召回">
            {eventsLoading
              ? "正在读取隔离原因…"
              : quarantine
                ? `${quarantine.reason}(${formatRelative(quarantine.at)})。`
                : "没有找到自动隔离的记录,可能是有人手动送审。"}
            通过审核后启用,置信度提升到至少 0.4。
          </Notice>
        </div>
      ) : detail.status === "rejected" ? (
        <div className="px-4 pt-4">
          <Notice tone="info" title="已拒绝,不参与召回">
            记忆仍保留在库里;把状态改回「启用」即可恢复召回。
          </Notice>
        </div>
      ) : null}

      <DetailSection>内容</DetailSection>
      <EditableRow label="建议" align="start" enterToSave {...rowProps("then")} display={<TextValue text={detail.suggestion} />}>
        <Textarea aria-label="建议" rows={3} value={draft.then} onChange={(event) => set("then", event.target.value)} />
      </EditableRow>
      <EditableRow
        label="原因"
        align="start"
        enterToSave
        hint="召回时和建议一起注入,帮助智能体判断这条记忆是否适用。"
        {...rowProps("because")}
        display={<TextValue text={detail.reason} />}
      >
        <Textarea aria-label="原因" rows={3} value={draft.because} onChange={(event) => set("because", event.target.value)} />
      </EditableRow>
      <EditableRow
        label="触发条件"
        align="start"
        hint="情境里出现关键词(不区分大小写)或匹配正则时,这条记忆进入召回候选。"
        {...rowProps("patterns")}
        display={<PatternsValue patterns={detail.pattern_list} />}
      >
        <PatternEditor value={draft.patterns} invalid={Boolean(rowError("patterns"))} onChange={(next) => set("patterns", next)} />
      </EditableRow>

      <Separator className="mt-2" />
      <DetailSection>分类</DetailSection>
      <EditableRow label="领域" enterToSave {...rowProps("domain")} display={<DomainLabel domain={detail.domain} />}>
        <DomainInput value={draft.domain} domains={domains} invalid={Boolean(rowError("domain"))} onChange={(next) => set("domain", next)} />
      </EditableRow>
      <EditableRow label="标签" align="start" {...rowProps("tags")} display={<ChipsValue items={detail.tags} />}>
        <ListEditor
          label="添加标签"
          placeholder="输入标签,回车或逗号添加"
          splitOnComma
          value={draft.tags}
          invalid={Boolean(rowError("tags"))}
          onChange={(next) => set("tags", next)}
        />
      </EditableRow>
      <EditableRow label="严重度" hint="危急级永远放行,不受剂量与不应期约束。" {...rowProps("severity")} display={<SeverityBadge severity={detail.severity} />}>
        <OptionSelect size="default" label="严重度" value={draft.severity} options={SEVERITY_OPTIONS} onChange={(value) => set("severity", value as Severity)} />
      </EditableRow>

      <Separator className="mt-2" />
      <DetailSection>学习状态</DetailSection>
      <EditableRow
        label="状态"
        hint="在这里改状态不会调整置信度;在隔离审核里「通过」会把置信度提升到至少 0.4。"
        {...rowProps("status")}
        display={<StatusBadge status={detail.status} />}
      >
        <OptionSelect size="default" label="状态" value={draft.status} options={STATUS_OPTIONS} onChange={(value) => set("status", value as EnzymeStatus)} />
      </EditableRow>
      <EditableRow
        label="置信度"
        hint="随每次反馈自动学习;手动修改会覆盖当前值,之后继续随反馈调整。"
        enterToSave
        {...rowProps("confidence")}
        display={<ConfidenceMeter value={detail.confidence} className="max-w-56" />}
      >
        <Input
          aria-label="置信度"
          inputMode="decimal"
          placeholder="0 ~ 1"
          value={draft.confidence}
          aria-invalid={Boolean(rowError("confidence")) || undefined}
          className="tabular-nums"
          onChange={(event) => set("confidence", event.target.value)}
        />
      </EditableRow>
      <DetailRow label="精度">
        {detail.stats.triggers ? (
          <span className="tabular-nums">
            {Math.round(detail.precision * 100)}%<span className="text-muted-foreground"> · 基于 {formatNumber(detail.stats.triggers)} 次反馈</span>
          </span>
        ) : (
          <span className="text-muted-foreground">还没有反馈</span>
        )}
      </DetailRow>
      <DetailRow label="质量分">
        <QualityScore value={detail.quality_score} />
      </DetailRow>

      <Separator className="mt-2" />
      <DetailSection>适用条件</DetailSection>
      <EditableRow
        label="前置条件"
        align="start"
        hint="情境里出现这些词时加分;严格模式(ranking.conditions_mode = hard)下必须全部出现才会召回。"
        {...rowProps("conditions")}
        display={<ChipsValue items={detail.conditions} />}
      >
        <ListEditor
          label="添加前置条件"
          placeholder="输入词语,回车添加"
          value={draft.conditions}
          invalid={Boolean(rowError("conditions"))}
          onChange={(next) => set("conditions", next)}
        />
      </EditableRow>
      <EditableRow
        label="排除条件"
        align="start"
        hint="情境里出现其中任意一个词,这条记忆就不会被召回。"
        {...rowProps("negations")}
        display={<ChipsValue items={detail.negations} />}
      >
        <ListEditor
          label="添加排除条件"
          placeholder="输入词语,回车添加"
          value={draft.negations}
          invalid={Boolean(rowError("negations"))}
          onChange={(next) => set("negations", next)}
        />
      </EditableRow>

      <Separator className="mt-2" />
      <DetailSection
        action={
          editing !== "references" ? (
            <Button type="button" variant="ghost" size="xs" disabled={saving !== null} onClick={() => {
              setDraft(toDraft(detail))
              setEditing("references")
            }}>
              <PencilIcon /> 编辑
            </Button>
          ) : null
        }
      >
        参考资料
      </DetailSection>
      {editing === "references" ? (
        <div data-editing="true" className="flex flex-col gap-2 px-4 pb-2" onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault()
            event.stopPropagation()
            cancelRow()
          }
        }}>
          <ListEditor
            layout="lines"
            label="添加参考资料"
            placeholder="文档链接或出处,回车添加"
            value={draft.references}
            invalid={Boolean(rowError("references"))}
            onChange={(next) => set("references", next)}
          />
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" onClick={cancelRow}>
              取消
            </Button>
            <Button type="button" size="sm" disabled={saving !== null} onClick={() => void saveRow("references")}>
              保存
            </Button>
          </div>
        </div>
      ) : detail.references.length ? (
        <ul className="flex flex-col gap-1 px-4 pb-2">
          {detail.references.map((reference, index) => (
            <li key={`${reference}-${index}`} className="min-w-0 text-sm">
              {/^https?:\/\//.test(reference) ? (
                <a href={reference} target="_blank" rel="noreferrer" className="inline-flex max-w-full items-center gap-1.5 text-primary underline-offset-2 hover:underline">
                  <ExternalLinkIcon className="size-3.5 shrink-0" aria-hidden="true" />
                  <span className="truncate">{reference.replace(/^https?:\/\//, "")}</span>
                </a>
              ) : (
                <span className="break-words">{reference}</span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="px-4 pb-2 text-sm text-muted-foreground">没有参考资料。</p>
      )}

      {quality ? (
        <>
          <Separator className="mt-2" />
          <DetailSection>质量门评分</DetailSection>
          <QualityBreakdown quality={quality} />
        </>
      ) : null}

      <Separator className="mt-2" />
      <DetailSection>统计</DetailSection>
      <div className="grid grid-cols-2 gap-2 px-4 pb-2 sm:grid-cols-4">
        <StatTile label="触发" value={formatNumber(detail.stats.triggers)} />
        <StatTile label="成功" value={formatNumber(detail.stats.successes)} tone={detail.stats.successes ? "success" : undefined} />
        <StatTile label="误报" value={formatNumber(detail.stats.false_positives)} tone={detail.stats.false_positives ? "destructive" : undefined} />
        <StatTile label="召回注入" value={formatNumber(detail.activation_count)} />
      </div>
      <DetailRow label="来源">
        <OriginBadge origin={detail.origin} />
      </DetailRow>
      <DetailRow label="类型">
        {ENZYME_TYPE_LABEL[detail.type] ?? detail.type} · 第 {detail.version} 版
      </DetailRow>
      {detail.parent_ids.length ? (
        <DetailRow label="父记忆">
          <span className="flex flex-wrap gap-2">
            {detail.parent_ids.map((id) => (
              <IdLink key={id} id={id} onOpen={onOpenEnzyme} />
            ))}
          </span>
        </DetailRow>
      ) : null}
      <DetailRow label="创建于">{formatDateTime(detail.created_at)}</DetailRow>
      <DetailRow label="更新于">{formatDateTime(detail.updated_at)}</DetailRow>
      <DetailRow label="最近召回">{detail.last_activated ? formatDateTime(detail.last_activated) : <NotSet>从未</NotSet>}</DetailRow>
      <DetailRow label="最近反馈">{detail.last_triggered ? formatDateTime(detail.last_triggered) : <NotSet>从未</NotSet>}</DetailRow>

      <Separator className="mt-2" />
      <DetailSection>
        经验样本
        <span className="ml-1.5 font-normal tabular-nums">{detail.exemplars.length}</span>
      </DetailSection>
      {detail.exemplars.length ? (
        <ExemplarList items={detail.exemplars} />
      ) : (
        <p className="px-4 pb-2 text-sm text-muted-foreground">还没有经验样本。被召回并得到成功反馈时,当时的情境会记在这里,之后遇到相似情境更容易想起它。</p>
      )}
      {detail.anti_exemplars.length ? (
        <>
          <DetailSection>
            反例
            <span className="ml-1.5 font-normal tabular-nums">{detail.anti_exemplars.length}</span>
          </DetailSection>
          <ExemplarList items={detail.anti_exemplars} anti />
        </>
      ) : null}

      {related.length ? (
        <>
          <Separator className="mt-2" />
          <DetailSection>关联记忆</DetailSection>
          <RelatedList related={related} onOpen={onOpenEnzyme} />
        </>
      ) : null}

      <Separator className="mt-2" />
      <DetailSection>进化历史</DetailSection>
      <EvolutionTimeline events={events} loading={eventsLoading} limit={eventsLimit} />
    </div>
  )
}
