"use client"

import { AdminOnly } from "@/components/admin-only"
import { SystemView } from "@/features/system/system-view"

export default function SystemPage() {
  return (
    <AdminOnly>
      <SystemView />
    </AdminOnly>
  )
}
