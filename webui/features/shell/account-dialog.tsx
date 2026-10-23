"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { KeyRoundIcon, MonitorIcon, MoonIcon, PaletteIcon, SunIcon, UploadIcon, Undo2Icon, UserIcon } from "lucide-react"
import { useTheme } from "next-themes"
import { useEffect, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { toast } from "sonner"
import { z } from "zod"

import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { ToneBadge } from "@/components/tone-badge"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput, InputGroupText } from "@/components/ui/input-group"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { avatarSrc } from "@/features/users/lib/users"
import { ApiError, errorMessage, request } from "@/lib/api/client"
import { authApi } from "@/lib/api/endpoints"
import { qk, useMe } from "@/lib/api/hooks"
import type { User } from "@/lib/api/types"
import { logout } from "@/lib/auth"
import { formatBytes, initials } from "@/lib/format"
import { labelOf } from "@/lib/labels"

/** 头像上传的限制,与后端 /api/auth/me/avatar 一致。 */
const AVATAR_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,image/png,image/jpeg,image/webp,image/gif"
const AVATAR_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".gif"]
const AVATAR_MAX_BYTES = 4 * 1024 * 1024
/** 新密码的最短长度,与后端一致。 */
const MIN_PASSWORD_LENGTH = 8

const profileSchema = z.object({
  displayName: z.string().trim().min(1, "显示名称不能为空").max(64, "显示名称不超过 64 个字符"),
})
type ProfileValues = z.infer<typeof profileSchema>

const passwordSchema = z
  .object({
    current: z.string().min(1, "请输入当前密码"),
    // 按原始值计长度:末尾的空格也是密码的一部分,和后端的判断一致
    next: z.string().min(MIN_PASSWORD_LENGTH, `新密码至少 ${MIN_PASSWORD_LENGTH} 位`),
    confirm: z.string().min(1, "请再输入一次新密码"),
  })
  .superRefine((values, ctx) => {
    if (values.confirm && values.confirm !== values.next) {
      ctx.addIssue({ code: "custom", path: ["confirm"], message: "两次输入的新密码不一致" })
    }
    if (values.current && values.next && values.current === values.next) {
      ctx.addIssue({ code: "custom", path: ["next"], message: "新密码不能与当前密码相同" })
    }
  })
type PasswordValues = z.infer<typeof passwordSchema>

export type AccountTab = "profile" | "security" | "appearance"

const TABS: { value: AccountTab; label: string; icon: React.ReactNode }[] = [
  { value: "profile", label: "资料", icon: <UserIcon aria-hidden="true" /> },
  { value: "security", label: "安全", icon: <KeyRoundIcon aria-hidden="true" /> },
  { value: "appearance", label: "外观", icon: <PaletteIcon aria-hidden="true" /> },
]

const THEMES = [
  { value: "light", label: "浅色", icon: SunIcon },
  { value: "dark", label: "深色", icon: MoonIcon },
  { value: "system", label: "跟随系统", icon: MonitorIcon },
] as const

/**
 * 改密码单独发请求而不用 authApi.changePassword:后端对「原密码错误」返回的是 401,
 * 共享封装遇到 401 会清掉会话并跳登录页——输错一次原密码就被登出。这里关掉自动跳转,自己区分两种 401。
 */
function changePassword(values: PasswordValues) {
  return request<{ ok: boolean }>("/api/auth/me/password", {
    method: "POST",
    body: { old_password: values.current, new_password: values.next },
    noRedirect: true,
  })
}

/** 账户设置:资料(显示名、头像)、安全(改密码)、外观(主题)。后端没有的东西不放。 */
export function AccountDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [tab, setTab] = useState<AccountTab>("profile")
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[88svh] gap-0 overflow-hidden p-0 sm:max-w-xl">
        <DialogHeader className="border-b px-6 py-4 pe-12">
          <DialogTitle>账户设置</DialogTitle>
          <DialogDescription>你的名字、头像和登录密码。</DialogDescription>
        </DialogHeader>
        {/* 表单状态放在对话框内容里:关闭时弹层卸载,下次打开从当前账户重新读起,取消就等于放弃修改 */}
        <AccountBody tab={tab} onTabChange={setTab} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  )
}

function AccountBody({ tab, onTabChange, onClose }: { tab: AccountTab; onTabChange: (tab: AccountTab) => void; onClose: () => void }) {
  const queryClient = useQueryClient()
  const me = useMe().data
  const authStatus = useQuery({ queryKey: ["auth-status"], queryFn: authApi.status, retry: false })
  // 未开启认证时后端用内置的开发者账户,资料与密码都不可修改
  const devMode = authStatus.data?.auth_required === false

  // ── 资料 ──
  const profileForm = useForm<ProfileValues>({
    resolver: zodResolver(profileSchema),
    defaultValues: { displayName: me?.display_name ?? "" },
    mode: "onTouched",
  })
  const [avatar, setAvatar] = useState<{ file: File; url: string } | null>(null)
  const [avatarError, setAvatarError] = useState<string | null>(null)

  // 预览用的 object URL 在换图或关闭时释放
  useEffect(() => {
    if (!avatar) return
    return () => URL.revokeObjectURL(avatar.url)
  }, [avatar])

  const saveProfile = useMutation({
    mutationFn: async ({ displayName, file }: { displayName: string; file: File | null }) => {
      let user: User | undefined
      if (file) user = await authApi.uploadAvatar(file)
      if (displayName !== (user ?? me)?.display_name) user = await authApi.updateProfile(displayName)
      return user
    },
    onSuccess: (user) => {
      if (user) {
        queryClient.setQueryData(qk.me, user)
        profileForm.reset({ displayName: user.display_name })
      }
      setAvatar(null)
      toast.success("资料已保存")
    },
    onError: (error) => toast.error(errorMessage(error)),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.me })
      // 用户列表里也显示名字与头像
      void queryClient.invalidateQueries({ queryKey: qk.users })
    },
  })
  const submitProfile = profileForm.handleSubmit((values) => saveProfile.mutate({ displayName: values.displayName, file: avatar?.file ?? null }))
  const profileDirty = profileForm.formState.isDirty || avatar !== null

  // ── 安全 ──
  const passwordForm = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { current: "", next: "", confirm: "" },
    mode: "onTouched",
  })
  const savePassword = useMutation({
    mutationFn: changePassword,
    onSuccess: () => {
      passwordForm.reset()
      toast.success("密码已修改", { description: "下次登录请使用新密码。" })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) {
        if (error.message.includes("原密码")) {
          passwordForm.setError("current", { message: "当前密码不正确" }, { shouldFocus: true })
          return
        }
        // 其他 401 是会话本身失效了
        toast.error("登录已过期,请重新登录")
        logout()
        return
      }
      if (error instanceof ApiError && error.status === 400) {
        passwordForm.setError("root", { message: error.message })
        return
      }
      toast.error(errorMessage(error))
    },
  })
  const submitPassword = passwordForm.handleSubmit((values) => savePassword.mutate(values))

  function pickAvatar(file: File | undefined) {
    if (!file) return
    const extension = `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`
    if (!AVATAR_EXTENSIONS.includes(extension)) {
      setAvatarError("仅支持 PNG、JPG、WebP 或 GIF 图片")
      return
    }
    if (file.size > AVATAR_MAX_BYTES) {
      setAvatarError(`图片 ${formatBytes(file.size)},超过了 4 MB 的上限`)
      return
    }
    setAvatarError(null)
    setAvatar({ file, url: URL.createObjectURL(file) })
  }

  const profileErrors = profileForm.formState.errors
  const passwordErrors = passwordForm.formState.errors

  return (
    <Tabs value={tab} onValueChange={(value) => onTabChange(value as AccountTab)} className="min-h-0 flex-1 gap-0">
      <div className="border-b px-6 py-2">
        <TabsList>
          {TABS.map((entry) => (
            <TabsTrigger key={entry.value} value={entry.value} className="gap-1.5 px-3">
              {entry.icon}
              {entry.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>

      <div className="max-h-[56svh] overflow-y-auto">
        <TabsContent value="profile" className="mt-0">
          <form id="account-profile-form" noValidate onSubmit={submitProfile}>
            <FieldGroup className="gap-5 px-6 py-5">
              {devMode ? <DevModeNotice /> : null}
              <Field>
                <FieldLabel>头像</FieldLabel>
                {me ? <AvatarPicker user={me} previewUrl={avatar?.url} disabled={devMode || saveProfile.isPending} onPick={pickAvatar} onRevert={() => { setAvatar(null); setAvatarError(null) }} /> : null}
                {avatarError ? <FieldError>{avatarError}</FieldError> : <FieldDescription className="text-xs">PNG、JPG、WebP 或 GIF,不超过 4 MB。保存后生效。</FieldDescription>}
              </Field>
              <Field data-invalid={Boolean(profileErrors.displayName) || undefined}>
                <FieldLabel htmlFor="account-display-name">显示名称</FieldLabel>
                <Input
                  id="account-display-name"
                  autoComplete="name"
                  disabled={devMode || saveProfile.isPending}
                  aria-invalid={Boolean(profileErrors.displayName) || undefined}
                  {...profileForm.register("displayName")}
                />
                {profileErrors.displayName ? <FieldError errors={[profileErrors.displayName]} /> : <FieldDescription className="text-xs">侧栏、审计日志与判定记录里显示的名字。</FieldDescription>}
              </Field>
              <Field>
                <FieldLabel htmlFor="account-username">用户名</FieldLabel>
                <InputGroup>
                  <InputGroupAddon align="inline-start">
                    <InputGroupText>@</InputGroupText>
                  </InputGroupAddon>
                  <InputGroupInput id="account-username" value={me?.username ?? ""} readOnly disabled />
                </InputGroup>
                <FieldDescription className="text-xs">用于登录,不可修改。</FieldDescription>
              </Field>
              <Field>
                <FieldLabel>角色</FieldLabel>
                <div>{me ? <ToneBadge tone={labelOf.role(me.role).tone} label={labelOf.role(me.role).label} /> : null}</div>
                <FieldDescription className="text-xs">由管理员在「用户」页调整。</FieldDescription>
              </Field>
            </FieldGroup>
          </form>
        </TabsContent>

        <TabsContent value="security" className="mt-0">
          <form id="account-password-form" noValidate onSubmit={submitPassword}>
            {/* 给密码管理器看的用户名字段:没有它,浏览器不知道新密码属于哪个账户 */}
            <input type="text" name="username" autoComplete="username" value={me?.username ?? ""} readOnly hidden />
            <FieldGroup className="gap-5 px-6 py-5">
              {devMode ? <DevModeNotice /> : null}
              {passwordErrors.root?.message ? (
                <Notice tone="destructive" title="密码没有修改">
                  {passwordErrors.root.message}
                </Notice>
              ) : null}
              <Field data-invalid={Boolean(passwordErrors.current) || undefined}>
                <FieldLabel htmlFor="account-password-current">当前密码</FieldLabel>
                <Input
                  id="account-password-current"
                  type="password"
                  autoComplete="current-password"
                  disabled={devMode || savePassword.isPending}
                  aria-invalid={Boolean(passwordErrors.current) || undefined}
                  {...passwordForm.register("current")}
                />
                <FieldError errors={[passwordErrors.current]} />
              </Field>
              <Field data-invalid={Boolean(passwordErrors.next) || undefined}>
                <FieldLabel htmlFor="account-password-next">新密码</FieldLabel>
                <Input
                  id="account-password-next"
                  type="password"
                  autoComplete="new-password"
                  disabled={devMode || savePassword.isPending}
                  aria-invalid={Boolean(passwordErrors.next) || undefined}
                  {...passwordForm.register("next")}
                />
                {passwordErrors.next ? <FieldError errors={[passwordErrors.next]} /> : <FieldDescription className="text-xs">至少 {MIN_PASSWORD_LENGTH} 位。</FieldDescription>}
              </Field>
              <Field data-invalid={Boolean(passwordErrors.confirm) || undefined}>
                <FieldLabel htmlFor="account-password-confirm">确认新密码</FieldLabel>
                <Input
                  id="account-password-confirm"
                  type="password"
                  autoComplete="new-password"
                  disabled={devMode || savePassword.isPending}
                  aria-invalid={Boolean(passwordErrors.confirm) || undefined}
                  {...passwordForm.register("confirm")}
                />
                <FieldError errors={[passwordErrors.confirm]} />
              </Field>
            </FieldGroup>
          </form>
        </TabsContent>

        <TabsContent value="appearance" className="mt-0">
          <FieldGroup className="gap-5 px-6 py-5">
            <AppearanceField />
          </FieldGroup>
        </TabsContent>
      </div>

      <DialogFooter className="border-t px-6 py-4">
        {tab === "appearance" ? (
          <Button type="button" onClick={onClose}>
            完成
          </Button>
        ) : (
          <>
            <Button type="button" variant="outline" onClick={onClose}>
              取消
            </Button>
            {tab === "profile" ? (
              <Button type="submit" form="account-profile-form" disabled={devMode || !profileDirty || saveProfile.isPending}>
                {saveProfile.isPending ? <Spinner /> : null}
                保存修改
              </Button>
            ) : (
              <Button type="submit" form="account-password-form" disabled={devMode || savePassword.isPending}>
                {savePassword.isPending ? <Spinner /> : null}
                修改密码
              </Button>
            )}
          </>
        )}
      </DialogFooter>
    </Tabs>
  )
}

function DevModeNotice() {
  return (
    <Notice tone="info" title="未开启登录认证">
      当前是后端内置的开发者账户,资料与密码不可修改。管理员可在「访问与安全」中开启认证。
    </Notice>
  )
}

function AppearanceField() {
  const { theme, setTheme } = useTheme()
  return (
    <Field>
      <FieldLabel>主题</FieldLabel>
      <ToggleGroup
        type="single"
        variant="outline"
        spacing={0}
        value={theme ?? "system"}
        // 再点一次已选中的项不应该变成「什么都没选」
        onValueChange={(next) => next && setTheme(next)}
        aria-label="主题"
        className="flex-wrap"
      >
        {THEMES.map(({ value, label, icon: Icon }) => (
          <ToggleGroupItem key={value} value={value} aria-label={label} className="gap-1.5 px-3">
            <Icon aria-hidden="true" />
            {label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <FieldDescription className="text-xs">只保存在这台设备的浏览器里。</FieldDescription>
    </Field>
  )
}

function AvatarPicker({
  user,
  previewUrl,
  disabled,
  onPick,
  onRevert,
}: {
  user: User
  previewUrl?: string
  disabled?: boolean
  onPick: (file: File | undefined) => void
  onRevert: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Avatar className="size-12">
        <AvatarImage src={previewUrl ?? avatarSrc(user.avatar_url)} alt="" />
        <AvatarFallback className="text-base">{initials(user.display_name || user.username)}</AvatarFallback>
      </Avatar>
      <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => inputRef.current?.click()}>
        <UploadIcon aria-hidden="true" />
        {user.avatar_url || previewUrl ? "更换头像" : "上传头像"}
      </Button>
      {previewUrl ? (
        <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={onRevert}>
          <Undo2Icon aria-hidden="true" /> 撤销
        </Button>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        accept={AVATAR_ACCEPT}
        className="sr-only"
        tabIndex={-1}
        aria-label="选择头像图片"
        onChange={(event) => {
          onPick(event.target.files?.[0])
          // 清空,否则再次选择同一个文件不会触发 change
          event.target.value = ""
        }}
      />
    </div>
  )
}
