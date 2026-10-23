import type { Metadata } from "next"
import { Suspense } from "react"

import { EnzymeManager } from "@/features/enzymes/components/enzyme-manager"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("记忆"),
  description: "浏览、检索、编辑与批量管理记忆:触发条件、建议、置信度与它们学到的经验。",
}

export default function EnzymesPage() {
  return (
    <>
      <h1 className="sr-only">记忆</h1>
      {/* 视图状态在 URL 里(useSearchParams),需要 Suspense 边界才能静态预渲染外层 */}
      <Suspense>
        <EnzymeManager />
      </Suspense>
    </>
  )
}
