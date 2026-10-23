"use client"

import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { AuditView } from "@/features/audit/audit-view"

export default function AuditPage() {
  return (
    <AdminOnly>
      {/* 视图用 useSearchParams 读取筛选、页码与打开的记录,需要 Suspense 边界 */}
      <Suspense>
        <AuditView />
      </Suspense>
    </AdminOnly>
  )
}
