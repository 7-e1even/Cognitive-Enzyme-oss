"use client"

import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { ProvidersView } from "@/features/models/providers-view"

export default function ModelProvidersPage() {
  return (
    <AdminOnly>
      {/* 视图用 useSearchParams 读取搜索词与选中的服务商,需要 Suspense 边界 */}
      <Suspense>
        <ProvidersView />
      </Suspense>
    </AdminOnly>
  )
}
