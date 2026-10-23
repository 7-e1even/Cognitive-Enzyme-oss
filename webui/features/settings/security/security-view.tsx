"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ArrowRightIcon, LockIcon, LockOpenIcon } from "lucide-react"
import Link from "next/link"
import { useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { Security, TokenKind } from "@/lib/api/types"

import { NewTokenDialog, SetTokenDialog, TOKEN_META } from "./token-dialogs"
import { TokenRow } from "./token-row"

/** 静态 API 令牌能调用的接口(与后端的机器令牌白名单一致)。 */
const MACHINE_ROUTES: { method: string; path: string; what: string }[] = [
  { method: "POST", path: "/api/prime", what: "召回记忆" },
  { method: "POST", path: "/api/capture", what: "会话捕获" },
  { method: "POST", path: "/api/report", what: "反馈" },
  { method: "POST", path: "/api/report_batch", what: "批量反馈" },
  { method: "POST", path: "/api/harvest", what: "沉淀经验" },
  { method: "POST", path: "/api/recall", what: "情景回想" },
  { method: "GET", path: "/api/enzymes", what: "读酶列表(园丁)" },
  { method: "GET", path: "/api/metrics", what: "学习指标(园丁)" },
  { method: "GET", path: "/api/episode/*", what: "读情景" },
  { method: "POST", path: "/api/v1/systemone", what: "System 1 网关" },
  { method: "POST", path: "/api/v1/systemone/feedback", what: "网关结果回传" },
]

const MCP_TOOLS = ["prime", "search", "recall", "report", "harvest"]

type McpState = { label: string; tone: "success-light" | "warning-light" | "destructive-light"; detail: string }

/**
 * MCP 认证的实际状态由两件事决定:数据库里有没有令牌、MCP 服务启动时有没有挂上校验器。
 * 校验器只能在启动时挂,所以「设了令牌但还没重启」和「清了令牌但还没重启」都是真实存在的中间态。
 */
function mcpState(security: Security): McpState {
  if (security.mcp_auth_active && security.mcp_token) {
    return { label: "认证已启用", tone: "success-light", detail: "MCP 客户端必须带这枚令牌;轮换即时生效。" }
  }
  if (security.mcp_auth_active && !security.mcp_token) {
    return {
      label: "拒绝所有请求",
      tone: "destructive-light",
      detail: "令牌已清除,但认证还挂着:重启前 MCP 拒绝一切调用,重启后切换为无认证模式。",
    }
  }
  if (!security.mcp_auth_active && security.mcp_token) {
    return { label: "待重启生效", tone: "warning-light", detail: "令牌已设置,但 MCP 以无认证模式启动;重启服务后才开始校验。" }
  }
  return {
    label: "无认证",
    tone: "warning-light",
    detail: `任何能访问 ${security.ports?.mcp ?? 8421} 端口的人都能调用 MCP 工具。对外暴露前请设置令牌并重启服务。`,
  }
}

/**
 * 访问与安全:REST 访问认证的总开关,和两枚机器令牌。
 * 两枚令牌都是明文存在数据库里的——管理员随时能看,所以「显示」是个开关而不是一次性的;
 * 但轮换的那一刻会把新令牌单独亮出来,因为那是最容易忘记去更新 hook 的时候。
 */
export function SecurityView() {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const securityQuery = useQuery({ queryKey: qk.security, queryFn: adminApi.security })
  const security = securityQuery.data
  const [revealed, setRevealed] = useState<Record<TokenKind, boolean>>({ api: false, mcp: false })
  const [fresh, setFresh] = useState<{ kind: TokenKind; token: string; restart: boolean } | null>(null)
  const [freshOpen, setFreshOpen] = useState(false)
  const [customKind, setCustomKind] = useState<TokenKind>("api")
  const [customOpen, setCustomOpen] = useState(false)

  const apply = (next: Security) => {
    queryClient.setQueryData(qk.security, next)
    void queryClient.invalidateQueries({ queryKey: qk.system })
  }

  const toggleAuth = useMutation({
    mutationFn: (enabled: boolean) => adminApi.updateSecurity(enabled),
    onSuccess: (next) => {
      apply(next)
      if (next.auth_enabled) toast.success("访问认证已开启", { description: "REST API 需要登录令牌或静态 API 令牌。" })
      else toast.warning("访问认证已关闭", { description: "整个 REST API 现在对所有能访问它的人开放。" })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "操作失败"),
  })

  const rotate = useMutation({
    mutationFn: (kind: TokenKind) => adminApi.rotateToken(kind),
    onSuccess: (next, kind) => {
      apply(next)
      setFresh({ kind, token: next[TOKEN_META[kind].field], restart: Boolean(next.restart_required) })
      setFreshOpen(true)
      setRevealed((current) => ({ ...current, [kind]: false }))
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "轮换失败"),
  })

  const clear = useMutation({
    mutationFn: (kind: TokenKind) => adminApi.clearToken(kind),
    onSuccess: (next, kind) => {
      apply(next)
      toast.success(`已清除${TOKEN_META[kind].label}`, { description: next.restart_required ? "需要重启服务后生效。" : "旧令牌立即失效。" })
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "清除失败"),
  })

  async function handleAuthChange(enabled: boolean) {
    if (!enabled) {
      const ok = await confirm({
        title: "关闭访问认证?",
        description:
          "关闭后,整个 REST API 不再要求登录:任何能访问后端端口的人都能读写记忆、改运行参数、看到服务商密钥与机器令牌、管理用户。仅在可信的本机环境里这样做。",
        confirmText: "仍然关闭",
        destructive: true,
      })
      if (!ok) return
    }
    toggleAuth.mutate(enabled)
  }

  async function handleRotate(kind: TokenKind) {
    const meta = TOKEN_META[kind]
    const ok = await confirm({
      title: `轮换${meta.label}?`,
      description:
        kind === "api"
          ? "服务端生成一枚新令牌,旧令牌立即失效:还在用旧令牌的 Claude Code hook、园丁和网关调用方会开始被拒绝,直到换上新令牌。"
          : "服务端生成一枚新令牌,旧令牌立即失效;MCP 客户端需要换上新令牌。",
      confirmText: "轮换",
      destructive: true,
    })
    if (ok) rotate.mutate(kind)
  }

  async function handleClear(kind: TokenKind) {
    const meta = TOKEN_META[kind]
    const ok = await confirm({
      title: `清除${meta.label}?`,
      description:
        kind === "api"
          ? "清除后没有任何机器令牌:认证开启时,hook、园丁与网关调用方都会被拒绝(园丁单独配置了令牌的除外)。"
          : security?.mcp_auth_active
            ? "令牌表立即清空:重启前 MCP 拒绝所有调用,重启后切换为无认证模式——任何能访问 MCP 端口的人都能调用。"
            : "清除后 MCP 保持无认证模式。",
      confirmText: "清除",
      destructive: true,
    })
    if (ok) clear.mutate(kind)
  }

  const mcp = security ? mcpState(security) : null
  // 库里的令牌与启动时挂上的校验器不一致(设了没挂、或挂着但清空了),就是在等重启
  const mcpPendingRestart = security ? security.mcp_auth_active !== Boolean(security.mcp_token) : false
  const busy = rotate.isPending || clear.isPending

  return (
    <div className="flex flex-col gap-4">
      {securityQuery.isLoading ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-28" />
          <Skeleton className="h-56" />
        </div>
      ) : securityQuery.error || !security ? (
        <ErrorState error={securityQuery.error} onRetry={() => void securityQuery.refetch()} />
      ) : (
        <>
          {!security.auth_enabled ? (
            <Notice
              tone="destructive"
              title="访问认证已关闭"
              action={
                <Button size="sm" variant="outline" disabled={toggleAuth.isPending} onClick={() => void handleAuthChange(true)}>
                  重新开启
                </Button>
              }
            >
              整个 REST API 当前不校验任何令牌:模型密钥、机器令牌、用户与全部配置对能访问后端端口的人开放。
            </Notice>
          ) : null}
          {mcpPendingRestart ? (
            <Notice
              tone="warning"
              title="MCP 令牌的改动需要重启服务"
              action={
                <Button size="sm" variant="outline" asChild>
                  <Link href="/admin/system">
                    系统状态 <ArrowRightIcon />
                  </Link>
                </Button>
              }
            >
              MCP 的 Bearer 校验器只能在服务启动时挂上或摘下。已启用认证时轮换令牌不需要重启。
            </Notice>
          ) : null}

          <Card>
            <CardContent className="flex items-center gap-4">
              <span className={security.auth_enabled ? "text-muted-foreground" : "text-destructive"}>
                {security.auth_enabled ? <LockIcon className="size-5" /> : <LockOpenIcon className="size-5" />}
              </span>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex items-center gap-2 text-sm font-medium">
                  要求登录
                  <ToneBadge tone={security.auth_enabled ? "success-light" : "destructive-light"} label={security.auth_enabled ? "开启" : "关闭"} />
                </div>
                <p className="text-sm text-muted-foreground">开启时,后台用登录令牌、hook 与园丁用静态 API 令牌;关闭后所有接口无需任何令牌。</p>
              </div>
              {toggleAuth.isPending ? <Spinner /> : null}
              <Switch checked={security.auth_enabled} disabled={toggleAuth.isPending} aria-label="要求登录" onCheckedChange={(checked) => void handleAuthChange(checked)} />
            </CardContent>
          </Card>

          <Card className="gap-0 py-0">
            <CardHeader className="border-b py-4">
              <CardTitle>机器令牌</CardTitle>
              <CardDescription>给无人值守的客户端用的长期令牌,不会过期;轮换后旧令牌立即失效。</CardDescription>
            </CardHeader>
            {(["api", "mcp"] as TokenKind[]).map((kind, index) => {
              const meta = TOKEN_META[kind]
              const token = security[meta.field]
              const status =
                kind === "api"
                  ? token
                    ? security.auth_enabled
                      ? { label: "生效中", tone: "success-light" as const }
                      : { label: "认证关闭,不校验", tone: "warning-light" as const }
                    : { label: "未设置", tone: "destructive-light" as const }
                  : { label: mcp?.label ?? "", tone: mcp?.tone ?? ("warning-light" as const) }
              return (
                <div key={kind}>
                  {index > 0 ? <Separator /> : null}
                  <TokenRow
                    label={meta.label}
                    status={status}
                    meta={kind === "api" ? ["Claude Code hooks", "夜间园丁", "网关调用方", "仅数据面接口"] : ["MCP 服务", "Bearer 令牌"]}
                    note={kind === "mcp" ? mcp?.detail : undefined}
                    token={token}
                    revealed={revealed[kind]}
                    busy={busy}
                    rotating={rotate.isPending && rotate.variables === kind}
                    onToggleReveal={() => setRevealed((current) => ({ ...current, [kind]: !current[kind] }))}
                    onRotate={() => void handleRotate(kind)}
                    onCustom={() => {
                      setCustomKind(kind)
                      setCustomOpen(true)
                    }}
                    onClear={() => void handleClear(kind)}
                  />
                </div>
              )
            })}
          </Card>

          <Card className="gap-0 py-0">
            <CardHeader className="border-b py-4">
              <CardTitle>令牌能做什么</CardTitle>
              <CardDescription>三种凭据,权限各不相同;机器令牌拿不到控制面。</CardDescription>
            </CardHeader>
            <div className="grid grid-cols-1 md:grid-cols-3 md:divide-x">
              <ScopeColumn
                title="静态 API 令牌"
                badge={<Badge variant="secondary">仅数据面</Badge>}
                description="写记忆、召回、反馈与网关调用;改不了任何配置,管理接口一律拒绝(403)。"
              >
                <ul className="flex flex-col gap-1">
                  {MACHINE_ROUTES.map((route) => (
                    <li key={`${route.method} ${route.path}`} className="flex min-w-0 items-center gap-2 text-xs">
                      <span className="w-9 shrink-0 font-mono text-[10px] text-muted-foreground">{route.method}</span>
                      <span className="min-w-0 truncate font-mono">{route.path}</span>
                      <span className="ms-auto shrink-0 text-muted-foreground">{route.what}</span>
                    </li>
                  ))}
                </ul>
              </ScopeColumn>
              <ScopeColumn
                title="MCP 令牌"
                badge={<Badge variant="outline">仅 MCP</Badge>}
                description="只作用于 MCP 服务,与 REST API 无关;调用下面五个工具。"
              >
                <div className="flex flex-wrap gap-1.5">
                  {MCP_TOOLS.map((tool) => (
                    <Badge key={tool} variant="outline" className="font-mono">
                      {tool}()
                    </Badge>
                  ))}
                </div>
              </ScopeColumn>
              <ScopeColumn
                title="登录令牌"
                badge={<Badge>控制面</Badge>}
                description="在后台登录后获得,按角色访问:管理员可以改配置与用户,操作员只能用记忆库与学习页。"
              >
                <Button size="xs" variant="outline" className="self-start" asChild>
                  <Link href="/admin/users">
                    管理用户 <ArrowRightIcon />
                  </Link>
                </Button>
              </ScopeColumn>
            </div>
          </Card>
        </>
      )}

      {fresh ? <NewTokenDialog kind={fresh.kind} token={fresh.token} restartRequired={fresh.restart} open={freshOpen} onOpenChange={setFreshOpen} /> : null}
      <SetTokenDialog kind={customKind} open={customOpen} onOpenChange={setCustomOpen} />
      {confirmDialog}
    </div>
  )
}

function ScopeColumn({
  title,
  badge,
  description,
  children,
}: {
  title: string
  badge: React.ReactNode
  description: string
  children: React.ReactNode
}) {
  return (
    <section className="flex min-w-0 flex-col gap-3 border-t p-4 first:border-t-0 md:border-t-0">
      <div className="flex flex-col gap-1">
        <h3 className="flex items-center gap-2 text-sm font-medium">
          {title}
          {badge}
        </h3>
        <p className="text-xs leading-relaxed text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  )
}
