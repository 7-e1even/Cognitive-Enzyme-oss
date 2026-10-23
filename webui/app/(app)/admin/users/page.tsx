"use client"

import { Suspense } from "react"

import { AdminOnly } from "@/components/admin-only"
import { UsersView } from "@/features/users/users-view"

export default function UsersPage() {
  return (
    <AdminOnly>
      {/* 视图用 useSearchParams 读取筛选与打开的成员,需要 Suspense 边界 */}
      <Suspense>
        <UsersView />
      </Suspense>
    </AdminOnly>
  )
}
