import type { Metadata } from "next"

import { FlowView } from "@/features/flow/components/flow-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("工作流程"),
  description: "召回、学习、巩固三条线的全貌:每一步做什么、现在的状态和数字,点一步去对应的页面。",
}

export default function FlowPage() {
  return (
    <>
      <h1 className="sr-only">工作流程</h1>
      <FlowView />
    </>
  )
}
