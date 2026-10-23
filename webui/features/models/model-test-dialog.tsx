"use client"

import { useMutation } from "@tanstack/react-query"
import { CircleCheckIcon, CircleXIcon, SendIcon } from "lucide-react"
import { useState } from "react"

import { JsonView } from "@/components/json-view"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Kbd } from "@/components/ui/kbd"
import { Progress } from "@/components/ui/progress"
import { Textarea } from "@/components/ui/textarea"
import { adminApi } from "@/lib/api/endpoints"
import type { ModelEntry, ProbeResult } from "@/lib/api/types"

import { KindBadge } from "./bits"
import { KIND_TEST_HINT } from "./lib/registry"

/**
 * 对一个模型发一次最小请求。每种类型问法不同:对话回一句话、向量报维度、精排给相关与无关文本打分、
 * 判官答一道是否题。失败是常态,所以结论放在最显眼处,服务商的原话原样给出。
 */
export function ModelTestDialog({
  model,
  open,
  onOpenChange,
}: {
  model: ModelEntry | null
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [prompt, setPrompt] = useState("")
  // 每次打开算一轮:上一轮(别的模型、或关掉前还在等的那次)的结果不显示在这一轮里
  const [session, setSession] = useState(0)
  const [seen, setSeen] = useState({ open, model })
  const mutation = useMutation({
    mutationFn: ({ id, text }: { id: string; text: string; session: number }) => adminApi.testModel(id, text),
  })
  if (seen.open !== open || seen.model !== model) {
    setSeen({ open, model })
    if (open) {
      setPrompt("")
      setSession((value) => value + 1)
    }
  }
  const current = mutation.variables?.session === session
  const pending = current && mutation.isPending
  const result = current ? mutation.data : undefined
  const hint = model ? KIND_TEST_HINT[model.kind] : null

  const run = () => {
    if (!model || pending) return
    mutation.mutate({ id: model.id, text: prompt.trim(), session })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2">
            测试模型 · <span className="font-mono">{model?.id}</span>
            {model ? <KindBadge kind={model.kind} /> : null}
          </DialogTitle>
          <DialogDescription>
            {model ? (
              <>
                {model.provider_name} · <span className="font-mono">{model.model}</span>。{hint?.explain}
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>
        {model && hint ? (
          <div className="flex min-w-0 flex-col gap-4">
            <Field>
              <FieldLabel htmlFor="model-test-prompt">{hint.label}</FieldLabel>
              <Textarea
                id="model-test-prompt"
                rows={3}
                placeholder={hint.placeholder}
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault()
                    run()
                  }
                }}
              />
              <FieldDescription className="text-xs">
                留空用占位文字。<Kbd>⌘</Kbd> <Kbd>Enter</Kbd> 发送。
              </FieldDescription>
            </Field>
            {pending ? (
              <div className="flex items-center gap-2 rounded-xl border border-dashed px-3 py-4 text-sm text-muted-foreground">
                <Spinner />
                正在等待 {model.provider_name} 的响应……对话模型最长可能需要一分钟。
              </div>
            ) : result ? (
              <TestOutcome model={model} result={result} />
            ) : null}
          </div>
        ) : null}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" size="sm" variant="outline">
              关闭
            </Button>
          </DialogClose>
          <Button type="button" size="sm" onClick={run} disabled={!model || pending}>
            {pending ? <Spinner /> : <SendIcon />}
            {result ? "再测一次" : "发送测试"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function TestOutcome({ model, result }: { model: ModelEntry; result: ProbeResult }) {
  const expectedDims = model.params?.dimensions
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <Notice
        tone={result.ok ? "success" : "destructive"}
        title={
          <span className="flex flex-wrap items-center gap-2">
            {result.ok ? <CircleCheckIcon className="size-4" /> : <CircleXIcon className="size-4" />}
            {result.ok ? "测试通过" : "测试失败"}
            <Badge variant="outline" className="tabular-nums">
              {result.latency_ms} ms
            </Badge>
            {result.model ? (
              <Badge variant="outline" className="font-mono">
                {result.model}
              </Badge>
            ) : null}
          </span>
        }
      >
        <p className="font-mono text-xs break-all whitespace-pre-wrap">{result.detail}</p>
      </Notice>
      {result.response !== undefined && result.response !== "" ? (
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">模型回复</span>
          <div className="max-h-60 overflow-y-auto rounded-xl border bg-muted/40 px-3 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
            {result.response}
          </div>
        </div>
      ) : null}
      {result.dimensions !== undefined ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">返回向量</span>
          <Badge variant="secondary" className="tabular-nums">
            {result.dimensions} 维
          </Badge>
          <span className="text-xs text-muted-foreground">
            {expectedDims ? `配置的维度:${expectedDims}` : "未配置维度,按返回值使用"}
          </span>
        </div>
      ) : null}
      {result.scores ? (
        <div className="flex flex-col gap-2 rounded-xl border px-3 py-2.5">
          <ScoreRow label="相关文本" hint="使用参数化查询,不要拼接用户输入到 SQL 语句里。" value={result.scores[0]} />
          <ScoreRow label="无关文本" hint="今天的天气适合出门散步。" value={result.scores[1]} />
        </div>
      ) : null}
      {result.answers ? (
        <div className="flex min-w-0 flex-col gap-1.5">
          <span className="text-xs font-medium text-muted-foreground">判官作答</span>
          <JsonView value={result.answers} />
        </div>
      ) : null}
    </div>
  )
}

function ScoreRow({ label, hint, value }: { label: string; hint: string; value: number | null | undefined }) {
  const score = value ?? null
  return (
    <div className="grid grid-cols-[4.5rem_1fr_9rem] items-center gap-3 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate text-xs text-muted-foreground" title={hint}>
        {hint}
      </span>
      <span className="flex items-center gap-2">
        <Progress value={score === null ? 0 : Math.max(0, Math.min(1, score)) * 100} className="h-1.5" />
        <span className="w-12 shrink-0 text-right text-xs tabular-nums">{score === null ? "—" : score.toFixed(3)}</span>
      </span>
    </div>
  )
}
