"use client"

import { zodResolver } from "@hookform/resolvers/zod"
import { useQuery } from "@tanstack/react-query"
import { Eye, EyeOff, Loader2, ServerCrash, TriangleAlert } from "lucide-react"
import { useRouter, useSearchParams } from "next/navigation"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { authApi } from "@/lib/api/endpoints"
import { errorMessage } from "@/lib/api/client"
import { getToken, setToken } from "@/lib/auth"

import { safeNextPath } from "./next-path"

const schema = z.object({
  username: z.string().trim().min(1, "请输入用户名"),
  password: z.string().min(1, "请输入密码"),
})
type Values = z.infer<typeof schema>

export function SignInForm() {
  const router = useRouter()
  const next = safeNextPath(useSearchParams().get("next"))
  const [show, setShow] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { username: "", password: "" } })

  // Already signed in (valid token) or the backend runs without auth: skip the form.
  const status = useQuery({ queryKey: ["auth-status"], queryFn: authApi.status, retry: false })
  const me = useQuery({
    queryKey: ["me"],
    queryFn: authApi.me,
    enabled: Boolean(getToken()) || status.data?.auth_required === false,
    retry: false,
  })
  useEffect(() => {
    if (me.data) router.replace(next)
  }, [me.data, next, router])

  const unreachable = status.isError ? errorMessage(status.error) : null
  const checking = status.isPending || (me.isFetching && !me.isError)

  async function onSubmit(values: Values) {
    setPending(true)
    setError(null)
    try {
      const res = await authApi.login(values.username, values.password)
      setToken(res.token)
      router.replace(next)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle className="text-xl">登录</CardTitle>
        <CardDescription>用管理员给你的账号登录。</CardDescription>
      </CardHeader>
      <CardContent>
        {checking ? (
          <div className="flex h-40 items-center justify-center gap-2 text-sm text-muted-foreground" role="status">
            <Loader2 className="size-4 animate-spin" /> 正在检查登录状态…
          </div>
        ) : (
          <form className="flex flex-col gap-5" noValidate onSubmit={form.handleSubmit(onSubmit)}>
            {unreachable ? (
              <Alert>
                <ServerCrash />
                <AlertTitle>连接不到后端服务</AlertTitle>
                <AlertDescription>{unreachable}</AlertDescription>
              </Alert>
            ) : null}
            <FieldGroup>
              <Field data-invalid={form.formState.errors.username ? true : undefined}>
                <FieldLabel htmlFor="username">用户名</FieldLabel>
                <Input
                  id="username"
                  autoComplete="username"
                  autoCapitalize="none"
                  autoFocus
                  disabled={pending}
                  {...form.register("username")}
                />
                <FieldError errors={[form.formState.errors.username]} />
              </Field>
              <Field data-invalid={form.formState.errors.password ? true : undefined}>
                <FieldLabel htmlFor="password">密码</FieldLabel>
                <InputGroup>
                  <InputGroupInput
                    id="password"
                    type={show ? "text" : "password"}
                    autoComplete="current-password"
                    disabled={pending}
                    {...form.register("password")}
                  />
                  <InputGroupAddon align="inline-end">
                    <InputGroupButton
                      type="button"
                      size="icon-xs"
                      aria-label={show ? "隐藏密码" : "显示密码"}
                      onClick={() => setShow((v) => !v)}
                    >
                      {show ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </InputGroupButton>
                  </InputGroupAddon>
                </InputGroup>
                <FieldError errors={[form.formState.errors.password]} />
              </Field>
            </FieldGroup>
            {error ? (
              <Alert variant="destructive" role="alert">
                <TriangleAlert />
                <AlertTitle>登录失败</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            ) : null}
            <Button type="submit" className="w-full" disabled={pending}>
              {pending ? <Loader2 className="animate-spin" /> : null}
              {pending ? "正在登录…" : "登录"}
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  )
}
