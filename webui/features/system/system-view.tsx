"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { DatabaseZapIcon, RefreshCwIcon, RotateCcwIcon } from "lucide-react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useSettings, useSystem } from "@/lib/api/hooks"
import { formatDuration, formatNumber } from "@/lib/format"

import { AuthCard, CapabilitiesCard, DependencyCards, HealthBadge, JobsCard, RuntimeCard, SourcesCard, VectorCard } from "./system-cards"
import { DEPENDENCIES } from "./lib/system"
import { TableSizes } from "./table-sizes"

import Link from "next/link"

/**
 * 系统状态:依赖连通性、运行环境、向量索引、后台任务、模型能力、访问认证、配置来源与数据表。
 * 每 15 秒自动刷新;两个运维动作放在顶部:重新载入配置、重建向量索引。
 */
export function SystemView() {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const system = useSystem()
  const settings = useSettings()
  const data = system.data
  const labelOfKey = (key: string) => settings.data?.settings.find((item) => item.key === key)?.label

  const reindex = useMutation({
    mutationFn: adminApi.reindex,
    onSuccess: (result) => {
      toast.success(`已排队 ${formatNumber(result.queued)} 条记忆`, {
        description: "后台会逐条重新计算嵌入写入向量索引,「等待同步」会逐渐降到 0。",
      })
      void queryClient.invalidateQueries({ queryKey: qk.system })
      void queryClient.invalidateQueries({ queryKey: qk.audit() })
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "重建失败"),
  })

  const reload = useMutation({
    mutationFn: adminApi.reload,
    onSuccess: (result) => {
      toast.success("配置已重新载入", {
        description: result.restart_required.length
          ? `运行参数与模型注册表已从数据库读回;仍有 ${result.restart_required.length} 项要重启服务才生效。`
          : "运行参数与模型注册表已从数据库读回,立即生效。",
      })
      for (const key of [qk.system, qk.settings, qk.roles, qk.providers, qk.models, qk.audit()]) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "重新载入失败"),
  })

  async function handleReindex() {
    const ok = await confirm({
      title: "重建向量索引?",
      description: `会把全部${data?.tables?.enzymes ? ` ${formatNumber(data.tables.enzymes.rows)} 条` : ""}记忆重新排队,由后台逐条重新计算嵌入并写入向量索引。期间召回可能暂时不完整;换了向量模型后必须做这一步。`,
      confirmText: "开始重建",
    })
    if (ok) reindex.mutate()
  }

  const failing = data ? DEPENDENCIES.filter((dependency) => !data.checks[dependency.key]) : []
  const restartKeys = data?.restart_required ?? []

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          {data ? failing.length ? <HealthBadge ok={false} badLabel={`${failing.length} 项异常`} /> : <HealthBadge ok okLabel="全部正常" /> : null}
          <p className="text-sm text-muted-foreground">
            {data ? `版本 ${data.version} · 已运行 ${formatDuration(data.uptime_s)} · 每 15 秒刷新` : "正在读取…"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" type="button" variant="outline" disabled={reload.isPending} onClick={() => reload.mutate()}>
            {reload.isPending ? <Spinner /> : <RotateCcwIcon />} 重新载入配置
          </Button>
          <Button size="sm" type="button" variant="outline" disabled={reindex.isPending} onClick={() => void handleReindex()}>
            {reindex.isPending ? <Spinner /> : <DatabaseZapIcon />} 重建向量索引
          </Button>
          <Button size="icon-sm" type="button" variant="outline" aria-label="刷新" disabled={system.isFetching} onClick={() => void system.refetch()}>
            <RefreshCwIcon className={system.isFetching ? "animate-spin" : undefined} aria-hidden="true" />
          </Button>
        </div>
      </div>

      {system.isError ? <ErrorState error={system.error} onRetry={() => void system.refetch()} /> : null}

      {restartKeys.length ? (
        <Notice
          tone="warning"
          title={`有 ${restartKeys.length} 项修改要重启服务才能生效`}
          action={
            <Button size="sm" variant="outline" asChild>
              <Link href="/settings/runtime">查看运行参数</Link>
            </Button>
          }
        >
          <p>这些参数已经保存,但只在服务启动时读取(向量库连接、向量维度一类)。「重新载入配置」管不到它们,重启 API 服务后生效。</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {restartKeys.map((key) => (
              <Badge key={key} variant="outline" className="gap-1.5 font-normal">
                {labelOfKey(key) ? <span>{labelOfKey(key)}</span> : null}
                <span className="font-mono text-muted-foreground">{key}</span>
              </Badge>
            ))}
          </div>
        </Notice>
      ) : null}

      {data?.postgresql_error ? (
        <Notice tone="destructive" title="PostgreSQL 连接失败">
          <span className="font-mono text-xs break-all">{data.postgresql_error}</span>
        </Notice>
      ) : null}
      {data?.qdrant_error ? (
        <Notice tone="destructive" title="Qdrant 连接失败">
          <span className="font-mono text-xs break-all">{data.qdrant_error}</span>
        </Notice>
      ) : null}

      <DependencyCards system={data} loading={system.isLoading} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {data ? (
          <>
            <RuntimeCard system={data} />
            <VectorCard system={data} />
            <JobsCard system={data} />
            <CapabilitiesCard system={data} />
            <AuthCard system={data} />
            <SourcesCard system={data} />
          </>
        ) : system.isLoading ? (
          Array.from({ length: 6 }).map((_, index) => <Skeleton key={index} className="h-52 rounded-xl" />)
        ) : null}
      </div>

      <TableSizes system={data} loading={system.isLoading} />
      {confirmDialog}
    </div>
  )
}
