import type { Metadata } from "next"

import { ConsolidationView } from "@/features/consolidation/components/consolidation-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("巩固调度"),
  description: "定期整理记忆库:衰减与合并、突变与交叉、DreamGlia 梦境演化与免疫巡逻、情景回放测试;设置自动调度或立即执行一次。",
}

export default function ConsolidationPage() {
  return (
    <>
      <h1 className="sr-only">巩固调度</h1>
      <ConsolidationView />
    </>
  )
}
