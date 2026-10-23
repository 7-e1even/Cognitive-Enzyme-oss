import type { Metadata } from "next"
import { Suspense } from "react"

import { OverviewDashboard } from "@/features/overview/components/overview-dashboard"
import { OverviewSkeleton } from "@/features/overview/components/overview-skeleton"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("详细指标"),
  description: "学习环的全部数字:库规模与构成、反馈覆盖率与加权精度、近期活动曲线、成熟记忆与最新进化事件。",
}

export default function MetricsPage() {
  return (
    <>
      <h1 className="sr-only">详细指标</h1>
      {/* 视图用 useSearchParams 读取时间范围;没有 Suspense 边界时静态预渲染会整页退回客户端渲染 */}
      <Suspense fallback={<OverviewSkeleton />}>
        <OverviewDashboard />
      </Suspense>
    </>
  )
}
