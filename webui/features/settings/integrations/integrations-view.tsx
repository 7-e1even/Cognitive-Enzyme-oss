"use client"

import { useQuery } from "@tanstack/react-query"
import { ArrowRightIcon, ChevronRightIcon, PlugIcon, RouteIcon, SquareTerminalIcon } from "lucide-react"
import Link from "next/link"
import { useState, useSyncExternalStore, type ReactNode } from "react"

import { CodeSnippet } from "@/components/code-snippet"
import { CopyButton } from "@/components/copy-button"
import { Notice } from "@/components/notice"
import { ToneBadge, type Tone } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useSettings } from "@/lib/api/hooks"
import type { Security } from "@/lib/api/types"
import { cn } from "@/lib/utils"

import {
  claudeCodeMcpCommand,
  claudeDesktopConfig,
  cursorConfig,
  endpointsFor,
  feedbackCurl,
  HOOK_GLOBAL_INSTALL,
  hookInstallCommand,
  hookSettingsJson,
  isLocalHost,
  SYSTEMONE_RESPONSE,
  systemOneCurl,
} from "../lib/snippets"

const API_PLACEHOLDER = "<CE_API_TOKEN>"
const MCP_PLACEHOLDER = "<MCP_TOKEN>"

const noopSubscribe = () => () => {}

/** 打开后台用的主机名;服务端渲染时没有 window,先按 localhost 出,水合后换成真实的。 */
function useBrowserHost() {
  return useSyncExternalStore(
    noopSubscribe,
    () => window.location.hostname || "localhost",
    () => "localhost",
  )
}

const MCP_TOOLS: { name: string; what: string }[] = [
  { name: "prime", what: "动手前调用:按当前情境召回相关的直觉与经验" },
  { name: "search", what: "自主检索记忆库(纯查询,不影响学习信号)" },
  { name: "recall", what: "回想历史情景:过去在类似情境里发生过什么" },
  { name: "report", what: "反馈 prime 返回的直觉是否有用,驱动记忆进化" },
  { name: "harvest", what: "把工作中发现的新经验沉淀为记忆" },
]

const HOOK_EVENTS: { event: string; command: string; timeout: string; what: string }[] = [
  { event: "UserPromptSubmit", command: "ce-prime-hook", timeout: "10 秒", what: "每次提问前推送相关记忆" },
  { event: "SessionEnd", command: "ce-capture-hook", timeout: "30 秒", what: "会话结束后提炼经验" },
  { event: "PreCompact", command: "ce-capture-hook", timeout: "30 秒", what: "上下文压缩前先提炼一次" },
]

type Status = { label: string; tone: Tone }

function hookStatus(security?: Security): Status | undefined {
  if (!security) return undefined
  if (!security.auth_enabled) return { label: "认证关闭,无需令牌", tone: "warning-light" }
  return security.api_token ? { label: "令牌已就绪", tone: "success-light" } : { label: "缺少令牌", tone: "destructive-light" }
}

function mcpStatus(security?: Security): Status | undefined {
  if (!security) return undefined
  if (security.mcp_auth_active && security.mcp_token) return { label: "认证已启用", tone: "success-light" }
  if (security.mcp_auth_active) return { label: "拒绝所有请求(待重启)", tone: "destructive-light" }
  if (security.mcp_token) return { label: "认证待重启生效", tone: "warning-light" }
  return { label: "无认证", tone: "warning-light" }
}

/**
 * 接入:把 Claude Code、MCP 客户端和别的 agent 接到这台服务上。
 * 所有片段都由上方的「服务器主机」和令牌开关现算:默认用占位符,需要时一键填入真实令牌,
 * 省得复制下来再手改,也不会在截图里无意漏出令牌。
 */
export function IntegrationsView() {
  const browserHost = useBrowserHost()
  const [hostInput, setHostInput] = useState<string | null>(null)
  const [realTokens, setRealTokens] = useState(false)
  const securityQuery = useQuery({ queryKey: qk.security, queryFn: adminApi.security })
  const settingsQuery = useSettings()
  const security = securityQuery.data

  const host = hostInput ?? browserHost
  const endpoints = endpointsFor(host, security?.ports)
  const local = isLocalHost(endpoints.host)
  const apiToken = realTokens && security?.api_token ? security.api_token : API_PLACEHOLDER
  const mcpHasToken = Boolean(security?.mcp_token)
  const mcpToken = mcpHasToken ? (realTokens ? (security?.mcp_token ?? MCP_PLACEHOLDER) : MCP_PLACEHOLDER) : null

  const route = settingsQuery.data?.settings.find((item) => item.key === "gateway.route")?.value
  const routeLength = Array.isArray(route) ? route.length : null

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>连接参数</CardTitle>
          <CardDescription>下面的命令与配置都按这里的地址和令牌生成。</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
          <Field>
            <FieldLabel htmlFor="integration-host">服务器主机</FieldLabel>
            <Input id="integration-host" className="font-mono" spellCheck={false} autoComplete="off" value={host} onChange={(event) => setHostInput(event.target.value)} />
            <FieldDescription className="text-xs">默认是你打开这个后台用的主机名。agent 跑在别的机器上时,填这台服务器对它们可达的地址。</FieldDescription>
          </Field>
          <div className="flex min-w-0 flex-col gap-3">
            <EndpointLine label="REST API" url={endpoints.apiUrl} />
            <EndpointLine label="MCP" url={endpoints.mcpUrl} />
            <Field orientation="horizontal" className="rounded-xl border px-3 py-2.5">
              <FieldContent>
                <FieldLabel htmlFor="integration-real-tokens">在片段里填入真实令牌</FieldLabel>
                <FieldDescription className="text-xs">
                  关闭时用 {API_PLACEHOLDER} / {MCP_PLACEHOLDER} 占位,复制后自己替换。
                </FieldDescription>
              </FieldContent>
              <Switch id="integration-real-tokens" checked={realTokens} disabled={!security} onCheckedChange={setRealTokens} />
            </Field>
          </div>
        </CardContent>
      </Card>

      {realTokens ? (
        <Notice tone="warning" title="片段里现在是真实令牌">
          复制、截图或贴进工单前留意:拿到静态 API 令牌就能读写记忆库。
        </Notice>
      ) : null}
      {security && !security.api_token && security.auth_enabled ? (
        <Notice
          tone="destructive"
          title="还没有静态 API 令牌"
          action={
            <Button size="sm" variant="outline" asChild>
              <Link href="/settings/security">
                去生成 <ArrowRightIcon />
              </Link>
            </Button>
          }
        >
          访问认证开着,hook 与网关调用方没有令牌会被拒绝。先生成一枚。
        </Notice>
      ) : null}

      <Section
        id="hooks"
        icon={<SquareTerminalIcon />}
        title="Claude Code Hooks"
        status={hookStatus(security)}
        description="每次提问前自动推送相关记忆;会话结束与压缩前自动提炼经验。"
      >
        <Tabs defaultValue="oneclick" className="min-w-0 gap-3">
          <TabsList>
            <TabsTrigger value="oneclick">一键安装</TabsTrigger>
            <TabsTrigger value="manual">手动配置</TabsTrigger>
          </TabsList>
          <TabsContent value="oneclick" className="flex min-w-0 flex-col gap-3">
            <CodeSnippet title="终端" language="bash" code={hookInstallCommand(endpoints.apiUrl, apiToken)} />
            <p className="text-xs leading-relaxed text-muted-foreground">
              写入 Claude Code 的全局配置;加 <span className="font-mono">--project</span> 改写当前项目的配置。装好后在 Claude Code 里打开一次 /hooks(或重启)加载。
            </p>
          </TabsContent>
          <TabsContent value="manual" className="flex min-w-0 flex-col gap-3">
            <CodeSnippet title="1. 全局安装两个命令" language="bash" code={HOOK_GLOBAL_INSTALL} />
            <CodeSnippet title="2. 写入 Claude Code 的 settings.json" language="json" code={hookSettingsJson(endpoints.apiUrl, apiToken)} />
            <p className="text-xs text-muted-foreground">3. 在 Claude Code 里打开一次 /hooks(或重启)加载。hook 永远不阻塞提问:服务不可达或超时就静默跳过。</p>
          </TabsContent>
        </Tabs>
        <Advanced>
          <div className="overflow-x-auto rounded-xl border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="ps-3">事件</TableHead>
                  <TableHead>命令</TableHead>
                  <TableHead>超时</TableHead>
                  <TableHead className="pe-3">作用</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {HOOK_EVENTS.map((row) => (
                  <TableRow key={row.event}>
                    <TableCell className="ps-3 font-mono text-xs">{row.event}</TableCell>
                    <TableCell className="font-mono text-xs">{row.command}</TableCell>
                    <TableCell className="text-xs tabular-nums">{row.timeout}</TableCell>
                    <TableCell className="pe-3 text-xs text-muted-foreground">{row.what}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          <p className="text-xs text-muted-foreground">
            可选环境变量:<span className="font-mono">CE_PRIME_TIMEOUT</span>(默认 3 秒)、<span className="font-mono">CE_PRIME_MAX</span>(每次最多注入 6 条)、
            <span className="font-mono">CE_PRIME_MIN_CONF</span>(最低置信度 0.4)。
          </p>
        </Advanced>
      </Section>

      <Section
        id="mcp"
        icon={<PlugIcon />}
        title="MCP 客户端"
        status={mcpStatus(security)}
        description="Claude Code、Claude Desktop、Cursor 经 Streamable HTTP 连接,按需调用五个工具。"
      >
        {!mcpHasToken && security ? (
          <Notice tone="warning" title="MCP 当前没有令牌">
            片段里不带 Authorization 头;对外暴露 MCP 端口前,先在「访问与安全」设置 MCP 令牌并重启服务。
          </Notice>
        ) : null}
        <Tabs defaultValue="claude-code" className="min-w-0 gap-3">
          <TabsList>
            <TabsTrigger value="claude-code">Claude Code</TabsTrigger>
            <TabsTrigger value="claude-desktop">Claude Desktop</TabsTrigger>
            <TabsTrigger value="cursor">Cursor</TabsTrigger>
          </TabsList>
          <TabsContent value="claude-code" className="flex min-w-0 flex-col gap-2">
            <CodeSnippet title="终端" language="bash" code={claudeCodeMcpCommand(endpoints.mcpUrl, mcpToken)} />
            <p className="text-xs text-muted-foreground">
              加 <span className="font-mono">--scope user</span> 在所有项目里可用;<span className="font-mono">claude mcp list</span> 查看连接状态。
            </p>
          </TabsContent>
          <TabsContent value="claude-desktop" className="flex min-w-0 flex-col gap-2">
            <CodeSnippet title="claude_desktop_config.json" language="json" code={claudeDesktopConfig(endpoints.mcpUrl, mcpToken, local)} />
            <p className="text-xs leading-relaxed text-muted-foreground">
              Claude Desktop 通过 mcp-remote 转接远程服务{local ? "" : ";非本机的 http 地址需要 --allow-http"},改完重启 Claude Desktop。
            </p>
          </TabsContent>
          <TabsContent value="cursor" className="flex min-w-0 flex-col gap-2">
            <CodeSnippet title="~/.cursor/mcp.json" language="json" code={cursorConfig(endpoints.mcpUrl, mcpToken)} />
            <p className="text-xs text-muted-foreground">
              也可以放在项目的 <span className="font-mono">.cursor/mcp.json</span>,只对该项目生效。
            </p>
          </TabsContent>
        </Tabs>
        <Advanced>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {MCP_TOOLS.map((tool) => (
              <div key={tool.name} className="flex min-w-0 flex-col gap-0.5 rounded-xl border px-3 py-2">
                <span className="font-mono text-sm font-medium">{tool.name}()</span>
                <span className="text-xs leading-relaxed text-muted-foreground">{tool.what}</span>
              </div>
            ))}
          </div>
        </Advanced>
      </Section>

      <Section
        id="gateway"
        icon={<RouteIcon />}
        title="System 1 网关"
        status={
          routeLength === null
            ? undefined
            : routeLength > 0
              ? { label: `路由 ${routeLength} 位判官`, tone: "success-light" }
              : { label: "路由为空", tone: "destructive-light" }
        }
        description="别的 agent 按 TypeSafe System One 协议提问(与 Jev 同款),网关按路由依次询问判官。"
        extra={
          <Button size="xs" variant="ghost" asChild>
            <Link href="/models/gateway">
              网关路由 <ArrowRightIcon />
            </Link>
          </Button>
        }
      >
        <CodeSnippet title="提问" language="bash" code={systemOneCurl(endpoints.apiUrl, apiToken)} />
        <CodeSnippet title="响应示例" language="json" code={SYSTEMONE_RESPONSE} />
        <Advanced>
          <CodeSnippet title="回传真实结果(可选)" language="bash" code={feedbackCurl(endpoints.apiUrl, apiToken)} />
          <ul className="flex list-disc flex-col gap-1 ps-5 text-xs leading-relaxed text-muted-foreground">
            <li>
              题型:<span className="font-mono">noul</span>(是否题,返回概率)、<span className="font-mono">choice</span>(单选,返回各选项概率)、
              <span className="font-mono">score</span>(评分)。
            </li>
            <li>
              请求里加 <span className="font-mono">&quot;model&quot;</span> 指定一位判官(注册名),不走路由;<span className="font-mono">&quot;agent&quot;</span> 写调用方标识,记进判定记录。
            </li>
            <li>回传:是否题给 true / false,单选题给正确选项的键;回传的结果会成为训练自己判官的标签。</li>
          </ul>
        </Advanced>
      </Section>
    </div>
  )
}

/** 一种接入方式:图标、名称、状态与一句话说明,下面是照抄即可的命令与配置。 */
function Section({
  id,
  icon,
  title,
  status,
  description,
  extra,
  children,
}: {
  id: string
  icon: ReactNode
  title: string
  status?: Status
  description: string
  extra?: ReactNode
  children: ReactNode
}) {
  return (
    <Card id={id} className="scroll-mt-4">
      <CardHeader className="flex-row items-start gap-3">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-muted/50 [&_svg]:size-4" aria-hidden="true">
          {icon}
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <CardTitle className="flex flex-wrap items-center gap-2">
            {title}
            {status ? <ToneBadge tone={status.tone} label={status.label} /> : null}
          </CardTitle>
          <CardDescription>{description}</CardDescription>
        </div>
        {extra}
      </CardHeader>
      <CardContent className="flex min-w-0 flex-col gap-4">{children}</CardContent>
    </Card>
  )
}

/** 折叠的「高级」:事件表、环境变量名、协议细节这类开发者才需要看的内容。 */
function Advanced({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex items-center gap-1 rounded-sm text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50">
        <ChevronRightIcon className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden="true" />
        高级
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-3 flex min-w-0 flex-col gap-3">{children}</CollapsibleContent>
    </Collapsible>
  )
}

function EndpointLine({ label, url }: { label: string; url: string }) {
  return (
    <div className="flex min-w-0 items-center gap-3 text-sm">
      <span className="w-16 shrink-0 text-xs text-muted-foreground">{label}</span>
      <code className="min-w-0 flex-1 truncate rounded-lg border bg-muted/50 px-2 py-1 font-mono text-xs">{url}</code>
      <CopyButton value={url} label={`${label} 地址`} />
    </div>
  )
}
