"use client"

import { AdminOnly } from "@/components/admin-only"
import { SecurityView } from "@/features/settings/security/security-view"

export default function SecuritySettingsPage() {
  return (
    <AdminOnly>
      <SecurityView />
    </AdminOnly>
  )
}
