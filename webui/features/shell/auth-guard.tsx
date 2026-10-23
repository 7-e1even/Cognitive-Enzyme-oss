"use client"

import { Loader2, RefreshCw, ServerCrash } from "lucide-react"
import { useRouter } from "next/navigation"
import { useEffect, type ReactNode } from "react"

import { Button } from "@/components/ui/button"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { ApiError, errorMessage } from "@/lib/api/client"
import { useMe } from "@/lib/api/hooks"
import { getToken, setSessionCookie } from "@/lib/auth"
import { authApi } from "@/lib/api/endpoints"
import { useQuery } from "@tanstack/react-query"

/** Render the shell only once we know who is signed in; an unreachable backend gets a retryable state. */
export function AuthGuard({ children }: { children: ReactNode }) {
  const router = useRouter()
  const status = useQuery({ queryKey: ["auth-status"], queryFn: authApi.status, retry: false })
  const needsLogin = status.data?.auth_required && !getToken()
  const me = useMe()

  useEffect(() => {
    if (needsLogin) router.replace(`/sign-in?next=${encodeURIComponent(window.location.pathname)}`)
  }, [needsLogin, router])
  useEffect(() => {
    if (me.data) setSessionCookie()
  }, [me.data])

  const failure = status.error ?? (me.error && !(me.error instanceof ApiError && me.error.status === 401) ? me.error : null)
  if (failure) {
    return (
      <div className="flex min-h-svh items-center justify-center p-6">
        <Empty className="max-w-md border">
          <EmptyHeader>
            <EmptyMedia variant="icon"><ServerCrash /></EmptyMedia>
            <EmptyTitle>连接不到后端服务</EmptyTitle>
            <EmptyDescription>{errorMessage(failure)}。请确认 REST API(默认端口 8420)已经启动,然后重试。</EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Button variant="outline" onClick={() => { void status.refetch(); void me.refetch() }}>
              <RefreshCw /> 重试
            </Button>
          </EmptyContent>
        </Empty>
      </div>
    )
  }
  if (needsLogin || (status.data?.auth_required && !me.data) || status.isPending) {
    return (
      <div className="flex min-h-svh items-center justify-center text-sm text-muted-foreground" role="status">
        <Loader2 className="mr-2 size-4 animate-spin" /> 正在进入控制台…
      </div>
    )
  }
  return <>{children}</>
}
