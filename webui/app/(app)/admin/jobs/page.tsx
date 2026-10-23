"use client"

import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { JobsView } from "@/features/jobs/jobs-view"

export default function JobsPage() {
  return (
    <AdminOnly>
      {/* 视图用 useSearchParams 读取筛选、页签与页码,需要 Suspense 边界 */}
      <Suspense>
        <JobsView />
      </Suspense>
    </AdminOnly>
  )
}
