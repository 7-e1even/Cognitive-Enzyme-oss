import type { Metadata } from "next"
import { Suspense } from "react"

import { EvolutionView } from "@/features/evolution/components/evolution-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("进化事件"),
  description: "记忆的每一次变化:反馈学习、会话沉淀、巩固合并、梦境演化、免疫巡逻与情景回放,按天排成时间线。",
}

export default function EvolutionPage() {
  return (
    <>
      <h1 className="sr-only">进化事件</h1>
      {/* 视图用 useSearchParams 读取类型与条数;没有 Suspense 边界时静态预渲染会整页退回客户端渲染 */}
      <Suspense>
        <EvolutionView />
      </Suspense>
    </>
  )
}
