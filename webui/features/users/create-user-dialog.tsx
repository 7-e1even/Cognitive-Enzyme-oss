"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { CircleCheckIcon } from "lucide-react"
import { useState } from "react"
import { Controller, useForm } from "react-hook-form"
import { toast } from "sonner"
import { z } from "zod"

import { CopyButton } from "@/components/copy-button"
import { Notice } from "@/components/notice"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet, FieldTitle } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { AdminUser, Role } from "@/lib/api/types"

import { PasswordInput } from "./password-input"
import { PASSWORD_MIN, ROLE_OPTIONS } from "./lib/users"

const createUserSchema = z.object({
  username: z.string().trim().min(1, "请输入用户名").max(64, "用户名不超过 64 个字符").regex(/^\S+$/, "用户名不能包含空格"),
  display_name: z.string().trim().max(64, "显示名不超过 64 个字符"),
  role: z.enum(["admin", "user"]),
  password: z.string().min(PASSWORD_MIN, `密码至少 ${PASSWORD_MIN} 位`),
})
type CreateUserValues = z.infer<typeof createUserSchema>

const BLANK: CreateUserValues = { username: "", display_name: "", role: "user", password: "" }

/**
 * 新建账号。本产品没有邮件邀请,管理员把初始密码转交给对方。
 * 建成后对话框不马上关,而是换成一张「登录信息」卡:密码只在这一刻看得到,关掉就只能重置。
 */
export function CreateUserDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (user: AdminUser) => void
}) {
  const queryClient = useQueryClient()
  const form = useForm<CreateUserValues>({
    resolver: zodResolver(createUserSchema),
    defaultValues: BLANK,
    mode: "onTouched",
  })
  const errors = form.formState.errors
  const [created, setCreated] = useState<{ user: AdminUser; password: string } | null>(null)

  const create = useMutation({
    mutationFn: (values: CreateUserValues) =>
      adminApi.createUser({ username: values.username, display_name: values.display_name, role: values.role, password: values.password }),
    onSuccess: (user, values) => {
      void queryClient.invalidateQueries({ queryKey: qk.users })
      setCreated({ user, password: values.password })
      onCreated(user)
      toast.success("账号已创建", { description: `${user.display_name}(@${user.username})` })
    },
    // 错误就地显示在对应字段上
    onError: (error) => {
      const message = errorMessage(error)
      if (error instanceof ApiError && message.includes("用户名")) form.setError("username", { message })
      else if (error instanceof ApiError && message.includes("密码")) form.setError("password", { message })
      else form.setError("root", { message })
    },
  })

  function handleOpenChange(next: boolean) {
    if (!next) {
      // 关闭即丢弃:下次打开是一张空表,已创建的密码也不再留在内存里
      form.reset(BLANK)
      setCreated(null)
      create.reset()
    }
    onOpenChange(next)
  }

  const submit = form.handleSubmit((values) => create.mutate(values))

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {created ? (
          <CreatedSummary user={created.user} password={created.password} onDone={() => handleOpenChange(false)} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>新建用户</DialogTitle>
              <DialogDescription>建好后把用户名和初始密码交给对方;对方登录后可以在账户设置里改密码。</DialogDescription>
            </DialogHeader>
            <form id="create-user-form" noValidate onSubmit={submit}>
              <FieldGroup className="gap-5">
                {errors.root ? (
                  <Notice tone="destructive" title="没能创建账号">
                    {errors.root.message}
                  </Notice>
                ) : null}
                <div className="grid gap-5 sm:grid-cols-2">
                  <Field data-invalid={Boolean(errors.username) || undefined}>
                    <FieldLabel htmlFor="create-user-username">用户名</FieldLabel>
                    <Input
                      id="create-user-username"
                      autoFocus
                      autoComplete="off"
                      spellCheck={false}
                      placeholder="例如 zhangsan"
                      aria-invalid={Boolean(errors.username) || undefined}
                      {...form.register("username")}
                    />
                    <FieldError errors={[errors.username]} />
                  </Field>
                  <Field data-invalid={Boolean(errors.display_name) || undefined}>
                    <FieldLabel htmlFor="create-user-display-name">显示名</FieldLabel>
                    <Input
                      id="create-user-display-name"
                      autoComplete="off"
                      placeholder="留空则与用户名相同"
                      aria-invalid={Boolean(errors.display_name) || undefined}
                      {...form.register("display_name")}
                    />
                    <FieldError errors={[errors.display_name]} />
                  </Field>
                </div>
                <Controller
                  control={form.control}
                  name="role"
                  render={({ field }) => (
                    <FieldSet className="gap-3">
                      <FieldLegend variant="label" className="mb-0">
                        角色
                      </FieldLegend>
                      <RadioGroup value={field.value} onValueChange={(value) => field.onChange(value as Role)} className="grid gap-2.5 sm:grid-cols-2">
                        {ROLE_OPTIONS.map((option) => (
                          <FieldLabel key={option.value} htmlFor={`create-user-role-${option.value}`}>
                            <Field orientation="horizontal" className="items-start">
                              <FieldContent>
                                <FieldTitle>{option.label}</FieldTitle>
                                <FieldDescription className="text-xs">{option.description}</FieldDescription>
                              </FieldContent>
                              <RadioGroupItem value={option.value} id={`create-user-role-${option.value}`} />
                            </Field>
                          </FieldLabel>
                        ))}
                      </RadioGroup>
                    </FieldSet>
                  )}
                />
                <Controller
                  control={form.control}
                  name="password"
                  render={({ field }) => (
                    <Field data-invalid={Boolean(errors.password) || undefined}>
                      <FieldLabel htmlFor="create-user-password">初始密码</FieldLabel>
                      <PasswordInput id="create-user-password" value={field.value} onChange={field.onChange} onBlur={field.onBlur} invalid={Boolean(errors.password)} />
                      {errors.password ? (
                        <FieldError errors={[errors.password]} />
                      ) : (
                        <FieldDescription className="text-xs">至少 {PASSWORD_MIN} 位;点「生成」得到一个随机密码。</FieldDescription>
                      )}
                    </Field>
                  )}
                />
              </FieldGroup>
            </form>
            <DialogFooter>
              <Button type="button" variant="outline" size="sm" onClick={() => handleOpenChange(false)}>
                取消
              </Button>
              <Button type="submit" form="create-user-form" size="sm" disabled={create.isPending}>
                {create.isPending ? <Spinner /> : null}
                创建账号
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function CreatedSummary({ user, password, onDone }: { user: AdminUser; password: string; onDone: () => void }) {
  const text = `用户名:${user.username}\n初始密码:${password}`
  return (
    <>
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2">
          <CircleCheckIcon className="size-5 text-emerald-600" aria-hidden="true" />
          账号已创建
        </DialogTitle>
        <DialogDescription>初始密码只在这一刻看得到,关掉对话框后只能重置。现在复制并交给对方。</DialogDescription>
      </DialogHeader>
      <dl className="grid grid-cols-[5rem_1fr] gap-x-4 gap-y-2 rounded-xl border bg-muted/40 px-3 py-3 text-sm">
        <dt className="text-muted-foreground">显示名</dt>
        <dd>{user.display_name}</dd>
        <dt className="text-muted-foreground">用户名</dt>
        <dd className="font-mono">{user.username}</dd>
        <dt className="text-muted-foreground">初始密码</dt>
        <dd className="font-mono break-all select-all">{password}</dd>
      </dl>
      <DialogFooter>
        <CopyButton value={text} label="登录信息" variant="outline" size="sm">
          复制登录信息
        </CopyButton>
        <Button type="button" size="sm" onClick={onDone}>
          完成
        </Button>
      </DialogFooter>
    </>
  )
}
