"use client"

import { useRouter } from "next/navigation"
import { useMemo, useState } from "react"

import { DataTable } from "@/components/data-table"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Hint } from "@/components/hint"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import type { PlaygroundResult } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"

import { createCandidateColumns } from "./candidate-columns"
import { hasAnalogy } from "./data"

/** 这次运行的全部候选:每一行一条记忆在各个阶段留下的信号。点一行打开记忆页里它的详情。 */
export function CandidateGrid({ result }: { result: PlaygroundResult }) {
  const router = useRouter()
  const [onlyInjected, setOnlyInjected] = useState(false)
  const candidates = result.candidates
  const injected = candidates.filter((c) => c.injected).length
  const data = useMemo(() => (onlyInjected ? candidates.filter((c) => c.injected) : candidates), [candidates, onlyInjected])
  const columns = useMemo(() => createCandidateColumns({ showAnalogy: hasAnalogy(candidates) }), [candidates])

  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle className="flex items-center gap-1.5">
          候选
          <Hint label="候选">
            召回:名次、词面命中、经验样本相似度;精排:相关分与是否低于下限;打分:综合分与分层;判官 p:这条记忆对请求直接有用的概率,单选 q:它是最有用那一条的概率;类比:与过往适用情境是否同一类;注入:入选路径,以及会不会被 hook 的置信度过滤丢掉。
          </Hint>
          <span className="ml-1 text-xs font-normal text-muted-foreground tabular-nums">
            共 {formatNumber(candidates.length)} 条 · 注入 {formatNumber(injected)} 条
          </span>
        </CardTitle>
        <div className="flex items-center gap-2">
          <Switch id="only-injected" size="sm" checked={onlyInjected} onCheckedChange={setOnlyInjected} />
          <Label htmlFor="only-injected" className="text-xs font-normal text-muted-foreground">
            只看注入
          </Label>
        </div>
      </CardHeader>
      <CardContent>
        <DataTable
          columns={columns}
          data={data}
          empty={onlyInjected ? "这一次没有注入任何记忆。" : "没有召回到任何候选。"}
          onRowClick={(c) => router.push(`/enzymes?id=${encodeURIComponent(c.id)}`)}
        />
      </CardContent>
    </Card>
  )
}
