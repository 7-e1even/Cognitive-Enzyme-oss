"use client"

import { AdminOnly } from "@/components/admin-only"
import { GatewayView } from "@/features/models/gateway-view"

export default function ModelGatewayPage() {
  return (
    <AdminOnly>
      <GatewayView />
    </AdminOnly>
  )
}
