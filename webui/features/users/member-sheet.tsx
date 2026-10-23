"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CircleCheckIcon, KeyRoundIcon, ScrollTextIcon, ShieldIcon, Trash2Icon } from "lucide-react"
import Link from "next/link"
import { useState, type ReactNode } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { CopyButton } from "@/components/copy-button"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { AdminUser, Role } from "@/lib/api/types"
import { formatDateTime, formatRelative, initials } from "@/lib/format"
import { labelOf } from "@/lib/labels"
import { cn } from "@/lib/utils"

import { PasswordInput } from "./password-input"
import { avatarSrc, PASSWORD_MIN, protectionReason, ROLE_OPTIONS } from "./lib/users"

type UserPatch = { display_name?: string; role?: Role; is_active?: boolean; password?: string }

/** 这次修改叫什么,成功提示和失败标题都用它。 */
function describePatch(patch: UserPatch, user: AdminUser) {
  if (patch.password) return "重置密码"
  if (patch.role) return `改为${labelOf.role(patch.role).label}`
  if (patch.is_active === true) return `启用 ${user.display_name}`
  if (patch.is_active === false) return `停用 ${user.display_name}`
  return "修改显示名"
}

function RoleBadge({ role }: { role: string }) {
  const label = labelOf.role(role)
  return (
    <ToneBadge tone={label.tone} className="gap-1">
      {role === "admin" ? <ShieldIcon aria-hidden="true" /> : null}
      {label.label}
    </ToneBadge>
  )
}

/**
 * 成员抽屉:显示名、角色、启用状态、重置密码与删除。
 * 这里的修改不走全局报错,而是把后端的拒绝原因写在抽屉顶部,和出问题的那一行放在一起。
 */
export function MemberSheet({
  user,
  open,
  onOpenChange,
  meId,
  activeAdmins,
  onRequestDelete,
}: {
  user: AdminUser | null
  open: boolean
  onOpenChange: (open: boolean) => void
  meId: string | undefined
  activeAdmins: number
  onRequestDelete: (user: AdminUser) => void
}) {
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [displayName, setDisplayName] = useState("")
  const [password, setPassword] = useState("")
  const [passwordTouched, setPasswordTouched] = useState(false)
  const [passwordReset, setPasswordReset] = useState(false)
  const [actionError, setActionError] = useState<{ title: string; message: string } | null>(null)
  const [seen, setSeen] = useState({ open, id: user?.id })

  // 换了一个人或重新打开:从头开始,不留上一次没做完的编辑和刚生成的密码
  if (seen.open !== open || seen.id !== user?.id) {
    setSeen({ open, id: user?.id })
    if (open) {
      setDisplayName(user?.display_name ?? "")
      setPassword("")
      setPasswordTouched(false)
      setPasswordReset(false)
      setActionError(null)
    }
  }

  const update = useMutation({
    mutationFn: (patch: UserPatch) => adminApi.updateUser(user!.id, patch),
    onMutate: () => setActionError(null),
    onSuccess: (next, patch) => {
      queryClient.setQueryData<{ users: AdminUser[] }>(qk.users, (old) =>
        old ? { users: old.users.map((item) => (item.id === next.id ? next : item)) } : old,
      )
      void queryClient.invalidateQueries({ queryKey: qk.users })
      void queryClient.invalidateQueries({ queryKey: qk.audit() })
      if (next.id === meId) void queryClient.invalidateQueries({ queryKey: qk.me })
      if (patch.password) {
        setPasswordReset(true)
        toast.success("密码已重置", { description: `把新密码交给 ${next.display_name}` })
      } else {
        toast.success(`已${describePatch(patch, next)}`, { description: `@${next.username}` })
      }
    },
    onError: (error, patch) => {
      if (user) setActionError({ title: `没能${describePatch(patch, user)}`, message: errorMessage(error) })
    },
  })

  const guard = user ? protectionReason(user, meId, activeAdmins) : null
  // 自己不能降级;最后一个管理员也不能降级。操作员升级为管理员不受限制。
  const roleLocked = Boolean(guard && (guard.self || guard.lastAdmin))
  const deactivateLocked = Boolean(guard && user?.is_active && (guard.self || guard.lastAdmin))
  const deleteLocked = Boolean(guard && (guard.self || guard.lastAdmin))
  const nameTooLong = displayName.trim().length > 64
  const nameDirty = Boolean(user) && displayName.trim() !== user!.display_name
  const passwordError = passwordTouched && password.length > 0 && password.length < PASSWORD_MIN ? `密码至少 ${PASSWORD_MIN} 位` : undefined

  async function toggleActive(next: boolean) {
    if (!user) return
    if (!next) {
      const ok = await confirm({
        title: `停用 ${user.display_name}?`,
        description: "对方的所有会话会立即失效,在重新启用之前无法登录。账号与操作记录都会保留。",
        confirmText: "停用",
        destructive: true,
      })
      if (!ok) return
    }
    update.mutate({ is_active: next })
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex flex-col gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-md">
        <SheetHeader className="shrink-0 gap-0 border-b p-0">
          <SheetTitle className="sr-only">成员</SheetTitle>
          <SheetDescription className="sr-only">查看与修改成员的显示名、角色、启用状态,重置密码或删除账号。</SheetDescription>
          {user ? (
            <div className="flex min-w-0 items-center gap-3 px-5 py-4 pe-12">
              <Avatar className={cn("size-11", !user.is_active && "opacity-50 grayscale")}>
                <AvatarImage src={avatarSrc(user.avatar_url)} alt="" />
                <AvatarFallback>{initials(user.display_name || user.username)}</AvatarFallback>
              </Avatar>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex min-w-0 items-center gap-2">
                  <h2 className="min-w-0 truncate text-lg font-semibold tracking-tight">{user.display_name}</h2>
                  <RoleBadge role={user.role} />
                  {!user.is_active ? <Badge variant="outline">已停用</Badge> : null}
                </div>
                <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
                  <span className="font-mono">@{user.username}</span>
                  <span>·</span>
                  <span>创建于 {formatRelative(user.created_at)}</span>
                  {user.id === meId ? (
                    <>
                      <span>·</span>
                      <span>这是你</span>
                    </>
                  ) : null}
                </p>
              </div>
            </div>
          ) : null}
        </SheetHeader>

        <ScrollArea className="min-h-0 flex-1">
          {user ? (
            <div className="flex flex-col gap-5 px-5 py-5">
              {actionError ? (
                <Notice tone="destructive" title={actionError.title}>
                  {actionError.message}
                </Notice>
              ) : null}
              {guard?.message ? (
                <Notice tone="info" title={guard.self ? "你自己的账号" : "最后一个管理员"}>
                  {guard.message}。
                </Notice>
              ) : null}

              <Section title="账号">
                <Field data-invalid={nameTooLong || undefined}>
                  <FieldLabel htmlFor="member-display-name">显示名</FieldLabel>
                  <div className="flex items-center gap-2">
                    <Input
                      id="member-display-name"
                      value={displayName}
                      placeholder={user.username}
                      aria-invalid={nameTooLong || undefined}
                      onChange={(event) => setDisplayName(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && nameDirty && !nameTooLong) update.mutate({ display_name: displayName.trim() })
                      }}
                    />
                    {nameDirty ? (
                      <Button size="sm" disabled={nameTooLong || update.isPending} onClick={() => update.mutate({ display_name: displayName.trim() })}>
                        {update.isPending && update.variables?.display_name !== undefined ? <Spinner /> : null}
                        保存
                      </Button>
                    ) : null}
                  </div>
                  {nameTooLong ? <FieldError>显示名不超过 64 个字符</FieldError> : <FieldDescription className="text-xs">侧栏、审计日志与操作记录里显示的名字;留空则显示用户名。</FieldDescription>}
                </Field>

                <Field>
                  <FieldLabel htmlFor="member-role">角色</FieldLabel>
                  {roleLocked ? (
                    <div className="flex items-center gap-2">
                      <RoleBadge role={user.role} />
                      <span className="text-xs text-muted-foreground">{guard?.self ? "不能取消自己的管理员身份" : "至少需要保留一个启用的管理员"}</span>
                    </div>
                  ) : (
                    <>
                      <Select value={user.role} disabled={update.isPending} onValueChange={(value) => value !== user.role && update.mutate({ role: value as Role })}>
                        <SelectTrigger id="member-role" className="w-full">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {ROLE_OPTIONS.map((option) => (
                              <SelectItem key={option.value} value={option.value}>
                                {option.label}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                      <FieldDescription className="text-xs">{ROLE_OPTIONS.find((option) => option.value === user.role)?.description}</FieldDescription>
                    </>
                  )}
                </Field>

                <Field orientation="horizontal" className="items-center justify-between rounded-xl border px-3 py-2.5">
                  <div className="flex flex-col gap-0.5">
                    <FieldLabel htmlFor="member-active">状态</FieldLabel>
                    <FieldDescription className="text-xs">
                      {deactivateLocked ? (guard?.self ? "不能停用自己" : "至少需要保留一个启用的管理员") : "停用后对方立即无法登录,已登录的会话也会失效"}
                    </FieldDescription>
                  </div>
                  <span className="flex items-center gap-2.5">
                    {update.isPending && update.variables?.is_active !== undefined ? <Spinner className="size-3.5" /> : null}
                    <span className={cn("text-sm", !user.is_active && "text-muted-foreground")}>{user.is_active ? "启用" : "已停用"}</span>
                    <Switch
                      id="member-active"
                      size="sm"
                      checked={user.is_active}
                      disabled={deactivateLocked || update.isPending}
                      aria-label={user.is_active ? "停用账号" : "启用账号"}
                      onCheckedChange={(checked) => void toggleActive(checked)}
                    />
                  </span>
                </Field>
              </Section>

              <Separator />

              <Section title="重置密码">
                <Field data-invalid={Boolean(passwordError) || undefined} className="gap-2">
                  <FieldLabel htmlFor="member-password" className="sr-only">
                    新密码
                  </FieldLabel>
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <PasswordInput
                        id="member-password"
                        value={password}
                        placeholder={`新密码,至少 ${PASSWORD_MIN} 位`}
                        invalid={Boolean(passwordError)}
                        disabled={update.isPending}
                        onChange={(value) => {
                          setPassword(value)
                          setPasswordReset(false)
                        }}
                        onBlur={() => setPasswordTouched(true)}
                      />
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      disabled={password.length < PASSWORD_MIN || update.isPending}
                      onClick={() => update.mutate({ password })}
                    >
                      {update.isPending && update.variables?.password ? <Spinner /> : <KeyRoundIcon />}
                      重置
                    </Button>
                  </div>
                  {passwordError ? (
                    <FieldError>{passwordError}</FieldError>
                  ) : passwordReset ? (
                    <p className="flex items-start gap-1.5 text-sm text-emerald-700 dark:text-emerald-400">
                      <CircleCheckIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                      新密码已生效。已登录的会话不会被踢下线,要立即收回访问请停用账号。
                    </p>
                  ) : (
                    <FieldDescription>生成或输入一个新密码,再把它交给对方。已登录的会话不受影响。</FieldDescription>
                  )}
                </Field>
              </Section>

              <Separator />

              <Section title="记录">
                <dl className="grid grid-cols-[6rem_1fr] gap-x-4 gap-y-2 text-sm">
                  <dt className="text-muted-foreground">用户 ID</dt>
                  <dd className="flex min-w-0 items-center gap-1">
                    <span className="truncate font-mono text-xs">{user.id}</span>
                    <CopyButton value={user.id} label="用户 ID" />
                  </dd>
                  <dt className="text-muted-foreground">创建时间</dt>
                  <dd>{formatDateTime(user.created_at)}</dd>
                  <dt className="text-muted-foreground">最后更新</dt>
                  <dd>{formatDateTime(user.updated_at)}</dd>
                </dl>
              </Section>
            </div>
          ) : null}
        </ScrollArea>

        <SheetFooter className="shrink-0 flex-row flex-wrap items-center justify-between gap-2 border-t bg-muted/40 px-5 py-3">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-destructive hover:text-destructive"
            disabled={!user || deleteLocked}
            title={deleteLocked ? (guard?.self ? "不能删除自己" : "至少需要保留一个启用的管理员") : undefined}
            onClick={() => user && onRequestDelete(user)}
          >
            <Trash2Icon /> 删除账号
          </Button>
          <Button size="sm" variant="outline" asChild>
            <Link href={user ? `/admin/audit?actor=${encodeURIComponent(user.username)}` : "/admin/audit"}>
              <ScrollTextIcon /> 查看操作记录
            </Link>
          </Button>
        </SheetFooter>
      </SheetContent>
      {confirmDialog}
    </Sheet>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-4">
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}
