"use client"

import { ShieldIcon } from "lucide-react"
import type { ReactNode } from "react"

import { EmptyState } from "@/components/states"
import { Skeleton } from "@/components/ui/skeleton"
import { useMe } from "@/lib/api/hooks"

/** 管理页的门:只有管理员能看,操作员看到一句说明而不是一堆 403。 */
export function AdminOnly({ children }: { children: ReactNode }) {
  const me = useMe()
  if (me.isPending) return <Skeleton className="h-40 w-full" />
  if (me.data?.role !== "admin") {
    return <EmptyState icon={ShieldIcon} title="需要管理员权限" description="这一页只对管理员开放。" />
  }
  return <>{children}</>
}
