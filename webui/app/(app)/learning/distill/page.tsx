import type { Metadata } from "next"
import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { DistillView } from "@/features/distill/components/distill-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("判定记录"),
  description: "每次 prime 的判定记录、事后真实结果与老师标注:训练团队自己的 System 1 判官所用的数据。",
}

export default function DistillPage() {
  return (
    <>
      <h1 className="sr-only">判定记录</h1>
      <AdminOnly>
        <Suspense>
          <DistillView />
        </Suspense>
      </AdminOnly>
    </>
  )
}
