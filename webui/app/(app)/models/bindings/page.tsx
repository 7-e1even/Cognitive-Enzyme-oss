"use client"

import { AdminOnly } from "@/components/admin-only"
import { BindingsView } from "@/features/models/bindings-view"

export default function ModelBindingsPage() {
  return (
    <AdminOnly>
      <BindingsView />
    </AdminOnly>
  )
}
