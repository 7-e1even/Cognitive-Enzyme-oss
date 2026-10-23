"use client"

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { EyeIcon, EyeOffIcon } from "lucide-react"
import Link from "next/link"
import { useState, type FormEvent } from "react"
import { toast } from "sonner"

import { Notice } from "@/components/notice"
import { OptionSelect } from "@/components/option-select"
import { Spinner } from "@/components/spinner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldContent, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet, FieldTitle } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Switch } from "@/components/ui/switch"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi, gardenerApi } from "@/lib/api/endpoints"
import { qk, useModels } from "@/lib/api/hooks"
import type { GardenerConfig, GardenerState } from "@/lib/api/types"

import { HOURS, MODE_LABEL, SECRET_MASK } from "../lib/gardener"

/**
 * 园丁设置(只给管理员)。
 *
 * 打开时从已保存的配置复制一份草稿,改的都是草稿,保存成功才回写页面;不保存就关掉,改动随之丢弃。
 * 两个令牌是只写的:已保存时只显示「已设置」,留空保持不变,要清除得勾选。
 * 模型优先从模型注册表里选 anthropic 协议的对话模型(地址与密钥跟着服务商走);
 * 注册表里没有合适的,再退回手填 ANTHROPIC_* 三项。
 */
type Secret = { value: string; clear: boolean }

interface Draft {
  mode: GardenerConfig["mode"]
  repo_path: string
  docker_host: string
  docker_image: string
  api_url: string
  modelSource: "registry" | "manual"
  model_id: string
  model: string
  base_url: string
  auth_token: Secret
  api_token: Secret
  max_enzymes: number | null
  timeout_minutes: number | null
  allowed_tools: string
  nightly_enabled: boolean
  nightly_hour: number
}

type Errors = Partial<Record<keyof Draft, string>>

function toDraft(config: GardenerConfig): Draft {
  return {
    mode: config.mode,
    repo_path: config.repo_path,
    docker_host: config.docker_host,
    docker_image: config.docker_image,
    api_url: config.api_url,
    // 没选注册表模型时停在「手动填写」:三项都空就是 Claude Code 自己的默认,这本身是合法配置
    modelSource: config.model_id ? "registry" : "manual",
    model_id: config.model_id,
    model: config.model,
    base_url: config.base_url,
    auth_token: { value: "", clear: false },
    api_token: { value: "", clear: false },
    max_enzymes: config.max_enzymes,
    timeout_minutes: config.timeout_minutes,
    allowed_tools: config.allowed_tools,
    nightly_enabled: config.nightly_enabled,
    nightly_hour: config.nightly_hour,
  }
}

/** 只写字段:勾了清除 → 空串;填了新值 → 新值;都没有 → 原样发回掩码(后端据此保留原值)。 */
function secretValue(secret: Secret, saved: string) {
  if (secret.clear) return ""
  if (secret.value.trim()) return secret.value.trim()
  return saved === SECRET_MASK ? SECRET_MASK : ""
}

const URL_PATTERN = /^https?:\/\/\S+$/
const DOCKER_HOST_PATTERN = /^(unix|tcp|https?):\/\/\S+$/

function validate(draft: Draft): Errors {
  const errors: Errors = {}
  if (draft.api_url.trim() && !URL_PATTERN.test(draft.api_url.trim())) errors.api_url = "需以 http:// 或 https:// 开头"
  if (draft.mode === "docker" && draft.docker_host.trim() && !DOCKER_HOST_PATTERN.test(draft.docker_host.trim())) {
    errors.docker_host = "只支持留空、unix://、tcp://、http(s)://"
  }
  if (draft.modelSource === "registry" && !draft.model_id) errors.model_id = "选一个模型,或改为手动填写"
  if (draft.modelSource === "manual" && draft.base_url.trim() && !URL_PATTERN.test(draft.base_url.trim())) errors.base_url = "需以 http:// 或 https:// 开头"
  if (draft.max_enzymes === null) errors.max_enzymes = "请填写 1–50 之间的整数"
  if (draft.timeout_minutes === null) errors.timeout_minutes = "请填写 5–120 之间的整数"
  if (!draft.allowed_tools.trim()) errors.allowed_tools = "至少要给 Read 一类的只读工具"
  return errors
}

function toPayload(draft: Draft, saved: GardenerConfig): Partial<GardenerConfig> {
  const registry = draft.modelSource === "registry"
  return {
    mode: draft.mode,
    repo_path: draft.repo_path.trim(),
    docker_host: draft.docker_host.trim(),
    docker_image: draft.docker_image.trim() || "ce-gardener",
    api_url: draft.api_url.trim(),
    model_id: registry ? draft.model_id : "",
    // 选了注册表模型时手填的三项作废,一并清空,免得日后切回手动时冒出过期的端点
    model: registry ? "" : draft.model.trim(),
    base_url: registry ? "" : draft.base_url.trim(),
    auth_token: registry ? "" : secretValue(draft.auth_token, saved.auth_token),
    api_token: secretValue(draft.api_token, saved.api_token),
    max_enzymes: draft.max_enzymes ?? saved.max_enzymes,
    timeout_minutes: draft.timeout_minutes ?? saved.timeout_minutes,
    allowed_tools: draft.allowed_tools.trim(),
    nightly_enabled: draft.nightly_enabled,
    nightly_hour: draft.nightly_hour,
  }
}

function numberOrNull(raw: string) {
  if (raw.trim() === "") return null
  const value = Number(raw)
  return Number.isFinite(value) ? Math.round(value) : null
}

function SecretField({
  id,
  label,
  saved,
  secret,
  onChange,
  placeholder,
  description,
}: {
  id: string
  label: string
  saved: string
  secret: Secret
  onChange: (next: Secret) => void
  placeholder: string
  description: string
}) {
  const [visible, setVisible] = useState(false)
  const isSet = saved === SECRET_MASK
  return (
    <Field>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <InputGroup>
        <InputGroupInput
          id={id}
          type={visible ? "text" : "password"}
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
          value={secret.value}
          disabled={secret.clear}
          placeholder={isSet ? "已设置 · 留空保持不变" : placeholder}
          onChange={(event) => onChange({ ...secret, value: event.target.value })}
        />
        <InputGroupAddon align="inline-end">
          <InputGroupButton type="button" size="icon-xs" aria-label={visible ? "隐藏" : "显示"} aria-pressed={visible} onClick={() => setVisible((current) => !current)}>
            {visible ? <EyeOffIcon aria-hidden="true" /> : <EyeIcon aria-hidden="true" />}
          </InputGroupButton>
        </InputGroupAddon>
      </InputGroup>
      <FieldDescription className="text-xs">{description}</FieldDescription>
      {isSet ? (
        <Field orientation="horizontal" className="mt-1">
          <Checkbox id={`${id}-clear`} checked={secret.clear} onCheckedChange={(checked) => onChange({ value: "", clear: checked === true })} />
          <FieldLabel htmlFor={`${id}-clear`} className="font-normal">
            清除已保存的值
          </FieldLabel>
        </Field>
      ) : null}
    </Field>
  )
}

export function GardenerSettingsSheet({ open, onOpenChange, config }: { open: boolean; onOpenChange: (open: boolean) => void; config: GardenerConfig }) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-xl">
        {/* 每次打开重新挂载表单:草稿总是从最新保存的配置开始 */}
        {open ? <SettingsForm config={config} onDone={() => onOpenChange(false)} /> : null}
      </SheetContent>
    </Sheet>
  )
}

function SettingsForm({ config, onDone }: { config: GardenerConfig; onDone: () => void }) {
  const queryClient = useQueryClient()
  // 后端实际端口(CE_API_PORT),占位提示里的默认地址按它写
  const apiPort = useQuery({ queryKey: qk.security, queryFn: adminApi.security }).data?.ports?.api ?? 8420
  const models = useModels()
  const [draft, setDraft] = useState<Draft>(() => toDraft(config))
  const [submitted, setSubmitted] = useState(false)
  const errors = submitted ? validate(draft) : {}
  // Claude Code 只说 Messages API:注册表里 anthropic 协议的对话模型才能给园丁用
  const candidates = (models.data ?? []).filter((model) => model.kind === "chat" && model.protocol === "anthropic")
  const missingModel = draft.model_id && models.data && !candidates.some((model) => model.id === draft.model_id)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((previous) => ({ ...previous, [key]: value }))

  const save = useMutation({
    mutationFn: (payload: Partial<GardenerConfig>) => gardenerApi.updateConfig(payload),
    onSuccess: (result) => {
      queryClient.setQueryData<GardenerState>(qk.gardener, (previous) => (previous ? { ...previous, config: result.config, command: result.command } : previous))
      void queryClient.invalidateQueries({ queryKey: [...qk.gardener, "check"] })
      void queryClient.invalidateQueries({ queryKey: qk.audit() })
      toast.success(result.message)
      onDone()
    },
  })

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setSubmitted(true)
    if (Object.keys(validate(draft)).length) return
    save.mutate(toPayload(draft, config))
  }

  const formError = save.error ? errorMessage(save.error) : null

  return (
    <>
      <SheetHeader className="gap-1 border-b pr-12">
        <SheetTitle>园丁设置</SheetTitle>
        <SheetDescription>模型端点与令牌经环境变量交给 Claude Code,不进提示词与日志。保存后下一次运行生效;环境自检读的是已保存的配置。</SheetDescription>
      </SheetHeader>

      <form id="gardener-settings" noValidate onSubmit={submit} className="flex min-h-0 flex-1 flex-col gap-7 overflow-y-auto px-4 py-4">
        {formError ? (
          <Notice tone="destructive" title="保存失败">
            {save.error instanceof ApiError && save.error.status === 403 ? "只有管理员可以修改园丁配置。" : formError}
          </Notice>
        ) : null}

        <FieldSet className="gap-4">
          <FieldLegend>运行方式</FieldLegend>
          <RadioGroup value={draft.mode} onValueChange={(value) => set("mode", value as Draft["mode"])} className="grid gap-2.5 sm:grid-cols-2">
            {(["docker", "local"] as const).map((mode) => (
              <FieldLabel key={mode} htmlFor={`gardener-mode-${mode}`}>
                <Field orientation="horizontal" className="items-start">
                  <FieldContent>
                    <FieldTitle>{MODE_LABEL[mode].label}</FieldTitle>
                    <FieldDescription className="text-xs">{MODE_LABEL[mode].description}</FieldDescription>
                  </FieldContent>
                  <RadioGroupItem value={mode} id={`gardener-mode-${mode}`} />
                </Field>
              </FieldLabel>
            ))}
          </RadioGroup>
          <FieldGroup className="gap-4">
            <Field>
              <FieldLabel htmlFor="gardener-repo">目标仓库路径</FieldLabel>
              <Input
                id="gardener-repo"
                className="font-mono"
                spellCheck={false}
                value={draft.repo_path}
                placeholder={draft.mode === "docker" ? "/srv/repos/my-app" : "D:\\code\\my-app 或 /home/me/my-app"}
                onChange={(event) => set("repo_path", event.target.value)}
              />
              <FieldDescription className="text-xs">
                {draft.mode === "docker" ? "Docker 主机上的路径(先把仓库 clone 到那台机器),只读挂载进容器。" : "后端所在机器上的路径,作为 claude 的工作目录。"}
                留空则园丁只整理记忆库,不对照代码。
              </FieldDescription>
            </Field>
            {draft.mode === "docker" ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field data-invalid={Boolean(errors.docker_host) || undefined}>
                  <FieldLabel htmlFor="gardener-docker-host">Docker 主机</FieldLabel>
                  <Input
                    id="gardener-docker-host"
                    className="font-mono"
                    spellCheck={false}
                    value={draft.docker_host}
                    placeholder="留空 = 本机 socket"
                    aria-invalid={Boolean(errors.docker_host) || undefined}
                    onChange={(event) => set("docker_host", event.target.value)}
                  />
                  {errors.docker_host ? <FieldError>{errors.docker_host}</FieldError> : <FieldDescription className="text-xs">远程主机填 tcp://ip:2375</FieldDescription>}
                </Field>
                <Field>
                  <FieldLabel htmlFor="gardener-docker-image">镜像</FieldLabel>
                  <Input id="gardener-docker-image" className="font-mono" spellCheck={false} value={draft.docker_image} placeholder="ce-gardener" onChange={(event) => set("docker_image", event.target.value)} />
                  <FieldDescription className="text-xs">需已在该主机上构建</FieldDescription>
                </Field>
              </div>
            ) : null}
            <Field data-invalid={Boolean(errors.api_url) || undefined}>
              <FieldLabel htmlFor="gardener-api-url">CE API 地址(园丁访问用)</FieldLabel>
              <Input
                id="gardener-api-url"
                className="font-mono"
                spellCheck={false}
                value={draft.api_url}
                placeholder={`留空 = http://${draft.mode === "docker" ? "host.docker.internal" : "127.0.0.1"}:${apiPort}`}
                aria-invalid={Boolean(errors.api_url) || undefined}
                onChange={(event) => set("api_url", event.target.value)}
              />
              {errors.api_url ? <FieldError>{errors.api_url}</FieldError> : null}
            </Field>
          </FieldGroup>
        </FieldSet>

        <Separator />

        <FieldSet className="gap-4">
          <FieldLegend>模型</FieldLegend>
          <RadioGroup value={draft.modelSource} onValueChange={(value) => set("modelSource", value as Draft["modelSource"])} className="grid gap-2.5 sm:grid-cols-2">
            {[
              { value: "registry", title: "从模型注册表选", hint: "地址与密钥跟着服务商走" },
              { value: "manual", title: "手动填写", hint: "ANTHROPIC_* 三项" },
            ].map((option) => (
              <FieldLabel key={option.value} htmlFor={`gardener-model-${option.value}`}>
                <Field orientation="horizontal" className="items-start">
                  <FieldContent>
                    <FieldTitle>{option.title}</FieldTitle>
                    <FieldDescription className="text-xs">{option.hint}</FieldDescription>
                  </FieldContent>
                  <RadioGroupItem value={option.value} id={`gardener-model-${option.value}`} />
                </Field>
              </FieldLabel>
            ))}
          </RadioGroup>
          {draft.modelSource === "registry" ? (
            <Field data-invalid={Boolean(errors.model_id) || undefined}>
              <FieldLabel htmlFor="gardener-model-id">对话模型</FieldLabel>
              <OptionSelect
                size="default"
                label="对话模型"
                value={draft.model_id}
                options={candidates.map((model) => ({ value: model.id, label: `${model.id} · ${model.provider_name}${model.enabled ? "" : " · 已停用"}` }))}
                onChange={(value) => set("model_id", value)}
                disabled={models.isLoading}
                className="w-full"
              />
              {errors.model_id ? <FieldError>{errors.model_id}</FieldError> : null}
              {missingModel ? <FieldError>之前选的模型「{draft.model_id}」已不在注册表里,或不再是 anthropic 协议的对话模型。</FieldError> : null}
              <FieldDescription className="text-xs">
                {models.data && candidates.length === 0 ? (
                  <>
                    注册表里还没有 anthropic 协议的对话模型。到{" "}
                    <Link href="/models/providers" className="text-foreground underline underline-offset-3">
                      服务商与模型
                    </Link>{" "}
                    添加一个(Anthropic、智谱 GLM、Kimi、MiniMax 等都提供兼容端点),或改为手动填写。
                  </>
                ) : (
                  "Claude Code 走 Messages API,只列 anthropic 协议的对话模型;夜间批任务用便宜的模型即可。"
                )}
              </FieldDescription>
            </Field>
          ) : (
            <FieldGroup className="gap-4">
              <div className="grid gap-4 sm:grid-cols-2">
                <Field>
                  <FieldLabel htmlFor="gardener-model">模型名</FieldLabel>
                  <Input id="gardener-model" className="font-mono" spellCheck={false} value={draft.model} placeholder="留空用 Claude Code 默认" onChange={(event) => set("model", event.target.value)} />
                  <FieldDescription className="text-xs">ANTHROPIC_MODEL</FieldDescription>
                </Field>
                <Field data-invalid={Boolean(errors.base_url) || undefined}>
                  <FieldLabel htmlFor="gardener-base-url">接口地址</FieldLabel>
                  <Input
                    id="gardener-base-url"
                    className="font-mono"
                    spellCheck={false}
                    value={draft.base_url}
                    placeholder="留空走官方"
                    aria-invalid={Boolean(errors.base_url) || undefined}
                    onChange={(event) => set("base_url", event.target.value)}
                  />
                  {errors.base_url ? <FieldError>{errors.base_url}</FieldError> : <FieldDescription className="text-xs">ANTHROPIC_BASE_URL</FieldDescription>}
                </Field>
              </div>
              <SecretField
                id="gardener-auth-token"
                label="模型令牌"
                saved={config.auth_token}
                secret={draft.auth_token}
                onChange={(next) => set("auth_token", next)}
                placeholder="ANTHROPIC_AUTH_TOKEN"
                description="ANTHROPIC_AUTH_TOKEN。只写不读,保存后不会再显示。"
              />
            </FieldGroup>
          )}
        </FieldSet>

        <Separator />

        <FieldSet className="gap-4">
          <FieldLegend>夜巡</FieldLegend>
          <FieldGroup className="gap-4">
            <Field orientation="horizontal">
              <FieldContent>
                <FieldLabel htmlFor="gardener-nightly">每晚自动运行</FieldLabel>
                <FieldDescription className="text-xs">由后端定时触发,按服务器本地时区的整点。</FieldDescription>
              </FieldContent>
              <Switch id="gardener-nightly" checked={draft.nightly_enabled} onCheckedChange={(checked) => set("nightly_enabled", checked)} />
            </Field>
            <div className="grid gap-4 sm:grid-cols-3">
              <Field data-disabled={!draft.nightly_enabled || undefined}>
                <FieldLabel htmlFor="gardener-hour">运行时间</FieldLabel>
                <OptionSelect size="default" label="运行时间" value={String(draft.nightly_hour)} options={HOURS} disabled={!draft.nightly_enabled} onChange={(value) => value !== "" && set("nightly_hour", Number(value))} className="w-full" />
              </Field>
              <Field data-invalid={Boolean(errors.max_enzymes) || undefined}>
                <FieldLabel htmlFor="gardener-max">每次审查上限</FieldLabel>
                <Input
                  id="gardener-max"
                  type="number"
                  min={1}
                  max={50}
                  step={1}
                  inputMode="numeric"
                  className="tabular-nums"
                  value={draft.max_enzymes ?? ""}
                  aria-invalid={Boolean(errors.max_enzymes) || undefined}
                  onChange={(event) => set("max_enzymes", numberOrNull(event.target.value))}
                />
                {errors.max_enzymes ? <FieldError>{errors.max_enzymes}</FieldError> : null}
              </Field>
              <Field data-invalid={Boolean(errors.timeout_minutes) || undefined}>
                <FieldLabel htmlFor="gardener-timeout">超时(分钟)</FieldLabel>
                <Input
                  id="gardener-timeout"
                  type="number"
                  min={5}
                  max={120}
                  step={5}
                  inputMode="numeric"
                  className="tabular-nums"
                  value={draft.timeout_minutes ?? ""}
                  aria-invalid={Boolean(errors.timeout_minutes) || undefined}
                  onChange={(event) => set("timeout_minutes", numberOrNull(event.target.value))}
                />
                {errors.timeout_minutes ? <FieldError>{errors.timeout_minutes}</FieldError> : null}
              </Field>
            </div>
            <FieldDescription className="-mt-2 text-xs">上限 1–50 条、超时 5–120 分钟;到时间整个进程被强制终止,已经回写的反馈保留。</FieldDescription>
            <Field data-invalid={Boolean(errors.allowed_tools) || undefined}>
              <FieldLabel htmlFor="gardener-tools">允许的工具</FieldLabel>
              <Input
                id="gardener-tools"
                className="font-mono"
                spellCheck={false}
                value={draft.allowed_tools}
                aria-invalid={Boolean(errors.allowed_tools) || undefined}
                onChange={(event) => set("allowed_tools", event.target.value)}
              />
              {errors.allowed_tools ? (
                <FieldError>{errors.allowed_tools}</FieldError>
              ) : (
                <FieldDescription className="text-xs">--allowedTools。默认只给只读工具和 curl(调 CE API 回写);不要放开写文件与任意命令。</FieldDescription>
              )}
            </Field>
          </FieldGroup>
        </FieldSet>

        <Separator />

        <FieldSet className="gap-4">
          <FieldLegend>回写身份</FieldLegend>
          <SecretField
            id="gardener-api-token"
            label="CE 静态 API 令牌"
            saved={config.api_token}
            secret={draft.api_token}
            onChange={(next) => set("api_token", next)}
            placeholder="留空使用「访问与安全」里的静态令牌"
            description="园丁唯一的写入通道(report / harvest),只能访问数据面接口。留空时自动使用本机的静态 API 令牌。"
          />
        </FieldSet>
      </form>

      <SheetFooter className="flex-row items-center justify-end border-t">
        <Button type="button" size="sm" variant="outline" onClick={onDone}>
          取消
        </Button>
        <Button type="submit" form="gardener-settings" size="sm" disabled={save.isPending}>
          {save.isPending ? <Spinner /> : null}
          保存设置
        </Button>
      </SheetFooter>
    </>
  )
}
