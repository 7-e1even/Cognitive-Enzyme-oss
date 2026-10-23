"use client"

import { AdminOnly } from "@/components/admin-only"
import { IntegrationsView } from "@/features/settings/integrations/integrations-view"

export default function IntegrationsPage() {
  return (
    <AdminOnly>
      <IntegrationsView />
    </AdminOnly>
  )
}
