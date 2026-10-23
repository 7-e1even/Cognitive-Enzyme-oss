import type { ReactNode } from "react"

import { DetailRow } from "@/components/detail-row"
import { ScoreBar } from "@/components/score-bar"
import { ToneBadge } from "@/components/tone-badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { PlaygroundResult } from "@/lib/api/types"

import { JUDGE_STATUS, LLM_JUDGE_STATUS } from "./data"

function routeText(route: PlaygroundResult["status"]["route"]) {
  if (!route) return "未分流"
  if (route.status !== "ok") return "判官超时或出错,全库召回"
  if (!route.domains.length) return "通用问题,全库召回"
  const picked = route.domains.map(([name, p]) => `${name} ${p.toFixed(2)}`).join(" · ")
  const mode = route.mode === "strict" ? "只在这些领域里找" : `补召回 ${route.added_ids.length} 条`
  return `${picked} · ${mode}`
}

function ms(value: number | null | undefined) {
  if (value === null || value === undefined) return "—"
  return value >= 1000 ? `${(value / 1000).toFixed(2)} 秒` : `${Math.round(value)} 毫秒`
}

function Value({ children }: { children: ReactNode }) {
  return <span className="break-words">{children}</span>
}

/** 判定状态:这一次运行报告出来的事实,不可编辑。 */
export function PrimeFacts({ result, className }: { result: PlaygroundResult; className?: string }) {
  const { status } = result
  const judge = JUDGE_STATUS[status.judge] ?? JUDGE_STATUS.off

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>判定状态</CardTitle>
        <CardDescription>这一次运行报告的事实</CardDescription>
      </CardHeader>
      <CardContent className="-mx-4 flex flex-col px-0">
        <DetailRow label="判官" hint="Jev(TypeSafe System One 协议)按校准过的概率决定注入哪几条;没作答时回退固定剂量。">
          <ToneBadge tone={judge.tone} label={judge.label} />
        </DetailRow>
        <DetailRow label="判官模型">
          <span className="font-mono text-xs">{status.judge_model || "—"}</span>
        </DetailRow>
        <DetailRow label="都没用" hint="单选题里「没有一条直接有用」的概率;低于静默门,补充、危急与兜底才可能入选。">
          <ScoreBar value={status.none} />
        </DetailRow>
        <DetailRow label="类比 · 都不是" hint="类比单选题里「没有一条过往情境是同一类」的概率。">
          <ScoreBar value={status.analogy_none} />
        </DetailRow>
        <DetailRow label="第二判官" hint="可选的 LLM 第二判官:与 Jev 并行判定,逐条概率取平均。">
          <Value>{status.llm_judge ? (LLM_JUDGE_STATUS[status.llm_judge] ?? status.llm_judge) : "未启用"}</Value>
        </DetailRow>
        <DetailRow label="精排">
          {status.rerank_failed ? <ToneBadge tone="warning-light" label="失败,已回退模式打分" /> : <Value>{result.stages.reranked === null ? "未执行" : "正常"}</Value>}
        </DetailRow>
        <DetailRow label="领域分流" hint="判官判断这次要用到哪些领域(与召回并行):专家通道再到这些领域里补召回并排在前面,严格模式只在这些领域里找。">
          <Value>{routeText(status.route)}</Value>
        </DetailRow>
        <DetailRow label="查询扩写" hint="LLM 把情境改写成更适合检索的说法,与原查询一起召回。">
          <Value>{status.expansion || "未扩写"}</Value>
        </DetailRow>
        <DetailRow label="判官等待">
          <Value>{ms(status.judge_wait_ms)}</Value>
        </DetailRow>
        <DetailRow label="总耗时" hint="从召回到注入的整条流水线;线上 hook 的总预算只有几秒。">
          <Value>{ms(result.elapsed_ms)}</Value>
        </DetailRow>
        <DetailRow label="检索查询" align="start" hint="由情境(与工作区特征)拼出的检索用查询。">
          <span className="line-clamp-4 text-xs leading-relaxed break-all text-muted-foreground">{result.query}</span>
        </DetailRow>
      </CardContent>
    </Card>
  )
}
