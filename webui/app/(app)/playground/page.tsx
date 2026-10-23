import type { Metadata } from "next"
import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { PlaygroundView } from "@/features/playground/components/playground-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("召回调试"),
  description: "输入一段情境,用线上 prime 同一条流水线跑一遍,逐阶段查看召回、精排、打分与判官的决定。",
}

export default function PlaygroundPage() {
  return (
    <>
      <h1 className="sr-only">召回调试</h1>
      <AdminOnly>
        <Suspense>
          <PlaygroundView />
        </Suspense>
      </AdminOnly>
    </>
  )
}
