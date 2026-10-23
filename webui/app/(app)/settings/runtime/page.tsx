"use client"

import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { RuntimeSettingsView } from "@/features/settings/runtime/runtime-view"

export default function RuntimeSettingsPage() {
  return (
    <AdminOnly>
      {/* 视图用 useSearchParams 读取搜索词与「显示高级」,需要 Suspense 边界 */}
      <Suspense>
        <RuntimeSettingsView />
      </Suspense>
    </AdminOnly>
  )
}
