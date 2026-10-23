"use client"

import { keepPreviousData, useQuery } from "@tanstack/react-query"
import { SearchIcon, SearchXIcon } from "lucide-react"
import Link from "next/link"
import { useState } from "react"

import { Hint } from "@/components/hint"
import { Notice } from "@/components/notice"
import { OptionSelect } from "@/components/option-select"
import { ScoreBar } from "@/components/score-bar"
import { EmptyState, ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { enzymeApi } from "@/lib/api/endpoints"
import { qk, useHealth } from "@/lib/api/hooks"
import type { SearchHit } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

const LIMIT_OPTIONS = [
  { value: "10", label: "10 条" },
  { value: "20", label: "20 条" },
  { value: "50", label: "50 条" },
]

function str(value: unknown) {
  return typeof value === "string" ? value : ""
}

function HitRow({ hit, rank }: { hit: SearchHit; rank: number }) {
  const score = typeof hit._score === "number" ? hit._score : null
  const severity = str(hit.severity)
  const status = str(hit.status)
  return (
    <li>
      <Link href={`/enzymes?id=${encodeURIComponent(hit.id)}`} className="flex items-start gap-3 rounded-lg px-3 py-2.5 transition-colors hover:bg-muted/50">
        <span className="w-6 shrink-0 pt-0.5 text-right text-xs text-muted-foreground tabular-nums">{rank}</span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="text-sm font-medium">{hit.then || hit.id}</p>
          {hit.patterns_text ? <p className="truncate font-mono text-xs text-muted-foreground">{hit.patterns_text}</p> : null}
          <div className="flex flex-wrap items-center gap-1.5">
            {hit.domain ? <span className="text-xs text-muted-foreground">{hit.domain}</span> : null}
            {severity && severity !== "info" ? <ToneBadge tone={labelOf.severity(severity).tone} label={labelOf.severity(severity).label} /> : null}
            {status && status !== "active" ? <ToneBadge tone={labelOf.status(status).tone} label={labelOf.status(status).label} /> : null}
            {typeof hit.confidence === "number" ? <span className="text-xs text-muted-foreground tabular-nums">置信度 {hit.confidence.toFixed(2)}</span> : null}
          </div>
        </div>
        {score !== null ? (
          <div className="shrink-0 pt-0.5">
            <ScoreBar value={score} />
          </div>
        ) : null}
      </Link>
    </li>
  )
}

/**
 * 语义检索:不经过判官与剂量,直接看「向量上最像的是哪几条」,用来判断召回这一步本身准不准。
 * 没有配置向量模型时后端退回关键词检索,结果不带相似度分数。
 */
export function SemanticSearch({ q, domain, onSearch }: { q: string; domain: string; onSearch: (q: string, domain: string) => void }) {
  const health = useHealth()
  const [draftQ, setDraftQ] = useState(q)
  const [draftDomain, setDraftDomain] = useState(domain)
  const [limit, setLimit] = useState("20")
  const [seen, setSeen] = useState({ q, domain })

  // URL 里的检索词变了(后退、分享链接)时同步输入框;渲染期对齐,不用 effect
  if (seen.q !== q || seen.domain !== domain) {
    setSeen({ q, domain })
    setDraftQ(q)
    setDraftDomain(domain)
  }

  const facets = useQuery({ queryKey: qk.facets, queryFn: enzymeApi.facets, staleTime: 60_000 })
  const domains = Object.keys(facets.data?.domain ?? {}).filter(Boolean)
  const search = useQuery({
    queryKey: ["search", { q, domain, limit }],
    queryFn: () => enzymeApi.search(q, domain || undefined, Number(limit)),
    enabled: Boolean(q.trim()),
    placeholderData: keepPreviousData,
  })
  const hits = search.data?.hits ?? []
  const vectorMode = hits.some((hit) => typeof hit._score === "number")

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-1.5">
            检索
            <Hint label="语义检索">按向量相似度直接找最像的记忆,不经过条件、精排、判官与剂量——用来单独检查召回这一步准不准。只返回「启用」的记忆。</Hint>
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-col gap-3 md:flex-row md:items-center"
            onSubmit={(event) => {
              event.preventDefault()
              if (draftQ.trim()) onSearch(draftQ.trim(), draftDomain.trim())
            }}
          >
            <Input value={draftQ} onChange={(event) => setDraftQ(event.target.value)} placeholder="输入一句情境或关键词,例如:asyncpg 写 jsonb 报错" aria-label="检索内容" className="min-w-0 flex-1" />
            <Input value={draftDomain} onChange={(event) => setDraftDomain(event.target.value)} placeholder="领域(不限)" aria-label="领域" list="search-domains" autoComplete="off" className="md:w-40" />
            <datalist id="search-domains">
              {domains.map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
            <OptionSelect size="default" label="返回条数" value={limit} options={LIMIT_OPTIONS} onChange={(value) => value && setLimit(value)} className="md:w-28" />
            <Button type="submit" disabled={!draftQ.trim()}>
              <SearchIcon /> 检索
            </Button>
          </form>
        </CardContent>
      </Card>

      {health.data?.embedding_configured === false ? (
        <Notice tone="warning" title="还没有配置向量模型">
          检索会退回数据库关键词匹配,结果没有相似度分数,也不代表线上的语义召回效果。
        </Notice>
      ) : null}

      {!q ? null : search.isError && !search.data ? (
        <ErrorState error={search.error} onRetry={() => void search.refetch()} title="检索失败" />
      ) : (
        <Card>
          <CardHeader className="flex-row items-center justify-between gap-2">
            <CardTitle className="flex items-center gap-2">
              结果
              <span className="text-xs font-normal text-muted-foreground tabular-nums">{search.data ? `${formatNumber(hits.length)} 条` : ""}</span>
            </CardTitle>
            {search.data && hits.length > 0 ? (
              <span className="flex items-center gap-1.5">
                <ToneBadge tone={vectorMode ? "info-light" : "secondary"} label={vectorMode ? "向量相似度" : "关键词匹配"} />
                <Hint label="分数说明">{vectorMode ? "分数是余弦相似度(越接近 1 越像)" : "没有向量可用,按关键词匹配,不带分数"}</Hint>
              </span>
            ) : null}
          </CardHeader>
          <CardContent className={cn("p-1.5", search.isPlaceholderData && "opacity-60")}>
            {search.isPending ? (
              <div className="flex flex-col gap-2 p-2">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-12 w-full" />
                ))}
              </div>
            ) : hits.length === 0 ? (
              <EmptyState icon={SearchXIcon} title="没有找到相关记忆" description="换个说法,或去掉领域限制再试。" />
            ) : (
              <ol className="flex flex-col">
                {hits.map((hit, index) => (
                  <HitRow key={hit.id} hit={hit} rank={index + 1} />
                ))}
              </ol>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
