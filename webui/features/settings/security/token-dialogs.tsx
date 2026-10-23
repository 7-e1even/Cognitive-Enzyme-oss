"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { DicesIcon, EyeIcon, EyeOffIcon, KeyRoundIcon } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { CopyButton } from "@/components/copy-button"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { TokenKind } from "@/lib/api/types"

export const TOKEN_META: Record<TokenKind, { label: string; field: "api_token" | "mcp_token" }> = {
  api: { label: "静态 API 令牌", field: "api_token" },
  mcp: { label: "MCP 令牌", field: "mcp_token" },
}

const MIN_LENGTH = 16

/** 浏览器里生成一枚随机令牌(与后端 token_urlsafe(32) 同等强度)。 */
export function randomToken(): string {
  const bytes = new Uint8Array(32)
  crypto.getRandomValues(bytes)
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

/** 轮换后把新令牌亮给人看——只在这一刻显眼地给出来,并说清楚接下来要去哪里更新。 */
export function NewTokenDialog({
  kind,
  token,
  restartRequired,
  open,
  onOpenChange,
}: {
  kind: TokenKind
  token: string
  restartRequired?: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const meta = TOKEN_META[kind]
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <KeyRoundIcon className="size-5 text-primary" aria-hidden="true" />
            新的{meta.label}
          </DialogTitle>
          <DialogDescription>旧令牌已经立即失效。现在复制,并更新所有用到它的地方。</DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-2">
          <div className="rounded-xl border bg-muted/50 px-3 py-3 font-mono text-sm break-all select-all">{token}</div>
          <CopyButton value={token} label={meta.label} variant="outline" size="sm" className="self-start">
            复制令牌
          </CopyButton>
        </div>
        <ul className="flex list-disc flex-col gap-1 ps-5 text-sm text-muted-foreground">
          {kind === "api" ? (
            <>
              <li>
                Claude Code hooks:更新 <span className="font-mono">~/.claude/settings.json</span> 里 env 的 <span className="font-mono">CE_API_TOKEN</span>,或重新运行一键安装命令。
              </li>
              <li>夜间园丁:没有单独配置令牌时自动改用新令牌;单独配置过的,到「夜间园丁」设置里更新。</li>
              <li>调用 System 1 网关的 agent 也要换成新令牌。</li>
            </>
          ) : (
            <li>
              MCP 客户端(Claude Code、Claude Desktop、Cursor)的 <span className="font-mono">Authorization: Bearer</span> 请求头换成新令牌。
            </li>
          )}
        </ul>
        {restartRequired ? (
          <Notice tone="warning" title="需要重启服务">
            MCP 当前以无认证模式运行,新令牌在服务重启、MCP 挂上认证之后才生效。
          </Notice>
        ) : null}
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" size="sm">
              我已复制
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** 自定义令牌:至少 16 个字符,也可以在浏览器里随机生成一枚。 */
export function SetTokenDialog({
  kind,
  open,
  onOpenChange,
}: {
  kind: TokenKind
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const meta = TOKEN_META[kind]
  const queryClient = useQueryClient()
  const [token, setToken] = useState("")
  const [reveal, setReveal] = useState(false)
  const [touched, setTouched] = useState(false)
  const [seen, setSeen] = useState({ open, kind })
  if (seen.open !== open || seen.kind !== kind) {
    setSeen({ open, kind })
    if (open) {
      setToken("")
      setReveal(false)
      setTouched(false)
    }
  }

  const trimmed = token.trim()
  const error =
    trimmed.length === 0
      ? "请输入令牌"
      : trimmed.length < MIN_LENGTH
        ? `至少 ${MIN_LENGTH} 个字符,现在 ${trimmed.length} 个`
        : /\s/.test(trimmed)
          ? "令牌里不能有空白字符"
          : undefined

  const mutation = useMutation({
    mutationFn: (value: string) => adminApi.setToken(kind, value),
    onSuccess: (security) => {
      queryClient.setQueryData(qk.security, security)
      void queryClient.invalidateQueries({ queryKey: qk.system })
      toast.success(`已更新${meta.label}`, {
        description: security.restart_required ? "需要重启服务后生效。" : "新令牌已生效,旧令牌立即失效。",
      })
      onOpenChange(false)
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : "保存失败"),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <form
          noValidate
          className="flex flex-col gap-6"
          onSubmit={(event) => {
            event.preventDefault()
            setTouched(true)
            if (!error) mutation.mutate(trimmed)
          }}
        >
          <DialogHeader>
            <DialogTitle>自定义{meta.label}</DialogTitle>
            <DialogDescription>用你自己的值替换当前令牌,例如从密钥管理系统里取的那一枚。保存后旧令牌立即失效。</DialogDescription>
          </DialogHeader>
          <Field data-invalid={(touched && Boolean(error)) || undefined}>
            <FieldLabel htmlFor="custom-token">令牌</FieldLabel>
            <InputGroup>
              <InputGroupInput
                id="custom-token"
                type={reveal ? "text" : "password"}
                autoComplete="off"
                spellCheck={false}
                className="font-mono"
                value={token}
                aria-invalid={(touched && Boolean(error)) || undefined}
                onChange={(event) => setToken(event.target.value)}
                onBlur={() => setTouched(true)}
              />
              <InputGroupAddon align="inline-end">
                <InputGroupButton size="icon-xs" aria-label={reveal ? "隐藏令牌" : "显示令牌"} onClick={() => setReveal((value) => !value)}>
                  {reveal ? <EyeOffIcon /> : <EyeIcon />}
                </InputGroupButton>
                <InputGroupButton
                  size="xs"
                  onClick={() => {
                    setToken(randomToken())
                    setReveal(true)
                    setTouched(true)
                  }}
                >
                  <DicesIcon /> 随机生成
                </InputGroupButton>
              </InputGroupAddon>
            </InputGroup>
            {touched && error ? <FieldError>{error}</FieldError> : <FieldDescription className="text-xs">至少 {MIN_LENGTH} 个字符,不含空白。</FieldDescription>}
          </Field>
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" size="sm" variant="outline">
                取消
              </Button>
            </DialogClose>
            <Button type="submit" size="sm" disabled={mutation.isPending}>
              {mutation.isPending ? <Spinner /> : null}
              保存令牌
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
