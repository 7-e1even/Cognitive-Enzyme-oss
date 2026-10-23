"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { TriangleAlertIcon } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Spinner } from "@/components/spinner"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { ApiError, errorMessage } from "@/lib/api/client"
import { adminApi } from "@/lib/api/endpoints"
import { qk } from "@/lib/api/hooks"
import type { ModelEntry, Provider } from "@/lib/api/types"

import { KindBadge, UsageBadges } from "./bits"
import { probeKey } from "./lib/queries"
import { usageLabel } from "./lib/registry"

/**
 * 删除前先说后果。服务商下挂着模型、模型正被某个环节引用时,后端会拒绝(409)并要求强制删除;
 * 这些后果前端已经知道,所以对话框一打开就直接列出来、按钮直接是「强制删除」。
 */
function invalidateRegistry(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: qk.providers })
  void queryClient.invalidateQueries({ queryKey: qk.models })
  void queryClient.invalidateQueries({ queryKey: qk.roles })
  void queryClient.invalidateQueries({ queryKey: qk.settings })
}

export function ProviderDeleteDialog({
  provider,
  models,
  open,
  onOpenChange,
  onDeleted,
}: {
  provider: Provider | null
  models: ModelEntry[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onDeleted: (provider: Provider, removedModels: string[]) => void
}) {
  const queryClient = useQueryClient()
  const attached = provider ? models.filter((m) => m.provider_id === provider.id) : []
  const [refusal, setRefusal] = useState<string | null>(null)
  const [seen, setSeen] = useState({ open, provider })
  if (seen.open !== open || seen.provider !== provider) {
    setSeen({ open, provider })
    if (open) setRefusal(null)
  }
  const force = attached.length > 0 || Boolean(refusal)

  const mutation = useMutation({
    mutationFn: (vars: { id: string; force: boolean }) => adminApi.deleteProvider(vars.id, vars.force),
    onSuccess: (result, vars) => {
      invalidateRegistry(queryClient)
      queryClient.removeQueries({ queryKey: probeKey(vars.id) })
      if (provider) onDeleted(provider, result.models ?? [])
      onOpenChange(false)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setRefusal(error.message)
        return
      }
      setRefusal(null)
      onOpenChange(false)
      toast.error(errorMessage(error))
    },
  })

  if (!provider) return null
  return (
    <AlertDialog open={open} onOpenChange={(next) => !mutation.isPending && onOpenChange(next)}>
      <AlertDialogContent className="sm:max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>删除服务商「{provider.name}」?</AlertDialogTitle>
          <AlertDialogDescription>
            {attached.length > 0
              ? `这家服务商下还有 ${attached.length} 个模型。强制删除会把它们一并删除,并解除全部引用:绑定它们的环节回退到环境变量配置,网关路由与影子判官里的条目被移除。`
              : "服务商的接口地址与密钥一并清除,删除后不可恢复。"}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {attached.length > 0 ? <ModelList models={attached} /> : null}
        {refusal ? <RefusalNote message={refusal} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>保留</AlertDialogCancel>
          <Button
            variant="destructive"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ id: provider.id, force })}
          >
            {mutation.isPending ? <Spinner /> : null}
            {force ? `强制删除${attached.length > 0 ? `(含 ${attached.length} 个模型)` : ""}` : "删除服务商"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function ModelDeleteDialog({
  model,
  open,
  onOpenChange,
  onDeleted,
}: {
  model: ModelEntry | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onDeleted: (model: ModelEntry, unbound: string[]) => void
}) {
  const queryClient = useQueryClient()
  const [refusal, setRefusal] = useState<string | null>(null)
  const [seen, setSeen] = useState({ open, model })
  if (seen.open !== open || seen.model !== model) {
    setSeen({ open, model })
    if (open) setRefusal(null)
  }
  const usedBy = model?.used_by ?? []
  const force = usedBy.length > 0 || Boolean(refusal)

  const mutation = useMutation({
    mutationFn: (vars: { id: string; force: boolean }) => adminApi.deleteModel(vars.id, vars.force),
    onSuccess: (result) => {
      invalidateRegistry(queryClient)
      if (model) onDeleted(model, result.unbound ?? [])
      onOpenChange(false)
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setRefusal(error.message)
        return
      }
      onOpenChange(false)
      toast.error(errorMessage(error))
    },
  })

  if (!model) return null
  return (
    <AlertDialog open={open} onOpenChange={(next) => !mutation.isPending && onOpenChange(next)}>
      <AlertDialogContent className="sm:max-w-lg">
        <AlertDialogHeader>
          <AlertDialogTitle>
            删除模型「<span className="font-mono">{model.id}</span>」?
          </AlertDialogTitle>
          <AlertDialogDescription>
            {usedBy.length > 0
              ? `它正被 ${usedBy.map(usageLabel).join("、")} 使用。强制删除会自动解除这些引用:角色回退到环境变量配置,列表参数里的条目被移除。`
              : "它没有被任何环节引用,删除后不可恢复。"}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {usedBy.length > 0 ? (
          <div className="flex flex-col gap-2 rounded-xl border px-3 py-2.5">
            <span className="text-xs text-muted-foreground">将被解除的引用</span>
            <UsageBadges usedBy={usedBy} />
          </div>
        ) : null}
        {refusal ? <RefusalNote message={refusal} /> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={mutation.isPending}>保留</AlertDialogCancel>
          <Button
            variant="destructive"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate({ id: model.id, force })}
          >
            {mutation.isPending ? <Spinner /> : null}
            {force ? "强制删除" : "删除模型"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function ModelList({ models }: { models: ModelEntry[] }) {
  return (
    <ul className="flex max-h-56 flex-col divide-y overflow-y-auto rounded-xl border">
      {models.map((m) => (
        <li key={m.id} className="flex min-w-0 flex-wrap items-center gap-2 px-3 py-2">
          <KindBadge kind={m.kind} />
          <span className="min-w-0 truncate font-mono text-sm">{m.id}</span>
          <UsageBadges usedBy={m.used_by} className="ms-auto" />
        </li>
      ))}
    </ul>
  )
}

function RefusalNote({ message }: { message: string }) {
  return (
    <p className="flex items-start gap-2 text-sm text-destructive">
      <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span>后端拒绝了普通删除:{message}</span>
    </p>
  )
}
