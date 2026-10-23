"use client"

import { useMutation, useQueryClient } from "@tanstack/react-query"
import { DatabaseZapIcon, PlusIcon, ServerIcon } from "lucide-react"
import { useCallback, useMemo, useState } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { EmptyState, ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Skeleton } from "@/components/ui/skeleton"
import { useSearchParamState } from "@/hooks/use-url-state"
import { copyText } from "@/lib/clipboard"
import { adminApi } from "@/lib/api/endpoints"
import { qk, useModels, useProviders } from "@/lib/api/hooks"
import type { ImportEnvResult, ModelEntry, Provider } from "@/lib/api/types"

import { ModelDeleteDialog, ProviderDeleteDialog } from "./delete-dialogs"
import { ImportEnvResultDialog } from "./import-env-dialog"
import { probeKey } from "./lib/queries"
import { guessKind, usageLabel } from "./lib/registry"
import { ModelFormSheet, type ModelFormInitial } from "./model-form-sheet"
import { ModelTestDialog } from "./model-test-dialog"
import { ProviderFormSheet, type ProviderSaved } from "./provider-form-sheet"
import { ProviderList } from "./provider-list"
import { ProviderPane, type ModelAction, type ProviderAction } from "./provider-pane"

/**
 * 服务商与模型:左栏服务商清单,右栏选中那家的密钥、地址和模型。
 * 选中的服务商记在地址栏(?p=),刷新、分享链接都停在同一家。各环节用哪个模型在「模型绑定」页。
 */
export function ProvidersView() {
  const providersQuery = useProviders()
  const modelsQuery = useModels()
  const queryClient = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()

  const [q, setQ] = useSearchParamState("q")
  const [selectedParam, setSelectedParam] = useSearchParamState("p")

  const [providerFormOpen, setProviderFormOpen] = useState(false)
  const [editingProvider, setEditingProvider] = useState<Provider | null>(null)
  const [modelFormOpen, setModelFormOpen] = useState(false)
  const [editingModel, setEditingModel] = useState<ModelEntry | null>(null)
  const [modelInitial, setModelInitial] = useState<ModelFormInitial | null>(null)
  const [testOpen, setTestOpen] = useState(false)
  const [testingModelId, setTestingModelId] = useState<string | null>(null)
  const [deleteProviderOpen, setDeleteProviderOpen] = useState(false)
  const [deletingProvider, setDeletingProvider] = useState<Provider | null>(null)
  const [deleteModelOpen, setDeleteModelOpen] = useState(false)
  const [deletingModel, setDeletingModel] = useState<ModelEntry | null>(null)
  const [importResult, setImportResult] = useState<ImportEnvResult | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  const [testingAll, setTestingAll] = useState(false)

  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data])
  const models = useMemo(() => modelsQuery.data ?? [], [modelsQuery.data])

  // 地址栏里的那家不存在(删了、手改了)就落到第一家,右栏永远有东西
  const selected = providers.find((p) => p.id === selectedParam) ?? providers[0] ?? null
  const selectedModels = useMemo(() => (selected ? models.filter((m) => m.provider_id === selected.id) : []), [models, selected])
  const testingModel = models.find((m) => m.id === testingModelId) ?? null

  const toggleProvider = useMutation({
    mutationFn: ({ provider, enabled }: { provider: Provider; enabled: boolean }) =>
      adminApi.updateProvider(provider.id, { enabled }),
    // 开关先动,失败再回滚
    onMutate: async ({ provider, enabled }) => {
      await queryClient.cancelQueries({ queryKey: qk.providers })
      const previous = queryClient.getQueryData<Provider[]>(qk.providers)
      queryClient.setQueryData<Provider[]>(qk.providers, (list) => list?.map((p) => (p.id === provider.id ? { ...p, enabled } : p)))
      return { previous }
    },
    onError: (error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(qk.providers, context.previous)
      toast.error(error instanceof Error ? error.message : "操作失败")
    },
    onSuccess: (_saved, { provider, enabled }) => {
      toast.success(enabled ? `已启用「${provider.name}」` : `已停用「${provider.name}」`)
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.providers })
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
    },
  })

  const toggleModel = useMutation({
    mutationFn: ({ model, enabled }: { model: ModelEntry; enabled: boolean }) => adminApi.updateModel(model.id, { enabled }),
    onMutate: async ({ model, enabled }) => {
      await queryClient.cancelQueries({ queryKey: qk.models })
      const previous = queryClient.getQueryData<ModelEntry[]>(qk.models)
      queryClient.setQueryData<ModelEntry[]>(qk.models, (list) => list?.map((m) => (m.id === model.id ? { ...m, enabled } : m)))
      return { previous }
    },
    onError: (error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(qk.models, context.previous)
      toast.error(error instanceof Error ? error.message : "操作失败")
    },
    onSuccess: (_saved, { model, enabled }) => {
      toast.success(enabled ? `已启用 ${model.id}` : `已停用 ${model.id}`)
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
    },
  })

  const importEnv = useMutation({
    mutationFn: adminApi.importEnv,
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: qk.providers })
      void queryClient.invalidateQueries({ queryKey: qk.models })
      void queryClient.invalidateQueries({ queryKey: qk.roles })
      void queryClient.invalidateQueries({ queryKey: qk.settings })
      setImportResult(result)
      setImportOpen(true)
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "导入失败"),
  })

  const openModelForm = useCallback((model: ModelEntry | null, initial?: ModelFormInitial) => {
    setEditingModel(model)
    setModelInitial(initial ?? null)
    setModelFormOpen(true)
  }, [])

  const openTest = useCallback((model: ModelEntry) => {
    setTestingModelId(model.id)
    setTestOpen(true)
  }, [])

  const handleProviderAction = useCallback(
    (action: ProviderAction, provider: Provider) => {
      if (action === "edit") {
        setEditingProvider(provider)
        setProviderFormOpen(true)
      } else if (action === "add-model") {
        openModelForm(null, { providerId: provider.id })
      } else {
        setDeletingProvider(provider)
        setDeleteProviderOpen(true)
      }
    },
    [openModelForm],
  )

  const handleModelAction = useCallback(
    (action: ModelAction, model: ModelEntry) => {
      if (action === "edit") openModelForm(model)
      else if (action === "test") openTest(model)
      else if (action === "copy") void copyText(model.id, "注册名")
      else {
        setDeletingModel(model)
        setDeleteModelOpen(true)
      }
    },
    [openModelForm, openTest],
  )

  async function handleProviderToggle(provider: Provider, enabled: boolean) {
    if (!enabled) {
      const used = models.filter((m) => m.provider_id === provider.id && m.used_by.length > 0)
      if (used.length > 0) {
        const usages = [...new Set(used.flatMap((m) => m.used_by))].map(usageLabel)
        const ok = await confirm({
          title: `停用「${provider.name}」?`,
          description: `它下面的 ${used.map((m) => m.id).join("、")} 正用于 ${usages.join("、")}。停用后这些环节回退到环境变量配置;没有环境变量配置的环节将不可用,网关路由会跳过它们。`,
          confirmText: "停用",
          destructive: true,
        })
        if (!ok) return
      }
    }
    toggleProvider.mutate({ provider, enabled })
  }

  async function handleModelToggle(model: ModelEntry, enabled: boolean) {
    if (!enabled && model.used_by.length > 0) {
      const ok = await confirm({
        title: `停用 ${model.id}?`,
        description: `它正用于 ${model.used_by.map(usageLabel).join("、")}。停用后这些环节回退到环境变量配置或不可用,网关路由与影子判官会跳过它。`,
        confirmText: "停用",
        destructive: true,
      })
      if (!ok) return
    }
    toggleModel.mutate({ model, enabled })
  }

  async function handleImport() {
    const ok = await confirm({
      title: "从环境变量导入模型配置?",
      description:
        "把环境变量里的旧模型配置导入注册表,并绑定到还没有在后台绑定的角色;老师配置里的模型也会注册。同协议、同地址、同密钥的连接复用已有服务商,不会重复创建。",
      confirmText: "导入",
    })
    if (ok) importEnv.mutate()
  }

  async function handleTestAll() {
    const targets = providers.filter((p) => p.enabled)
    if (targets.length === 0) {
      toast.info("没有启用的服务商可测")
      return
    }
    setTestingAll(true)
    const results = await Promise.all(
      targets.map((p) =>
        queryClient
          .fetchQuery({
            queryKey: probeKey(p.id),
            queryFn: () => adminApi.testProvider(p.id),
            retry: false,
            staleTime: 0,
            gcTime: 30 * 60_000,
          })
          .then((r) => r.ok)
          .catch(() => false),
      ),
    )
    setTestingAll(false)
    const passed = results.filter(Boolean).length
    const failed = results.length - passed
    if (failed === 0) toast.success(`${passed} 家服务商全部连通`)
    else toast.warning(`${passed} 家连通,${failed} 家失败`, { description: "在左栏点开那家,右栏显示失败原因。" })
  }

  function handleProviderSaved(result: ProviderSaved) {
    const { provider, created, models: createdModels, modelErrors } = result
    if (!created) {
      toast.success(`已保存「${provider.name}」`)
      return
    }
    toast.success(`已创建服务商「${provider.name}」`, {
      description: createdModels.length > 0 ? `同时注册了 ${createdModels.join("、")}。` : "接着测试连接或注册模型。",
    })
    if (modelErrors.length > 0) {
      toast.error(`${modelErrors.length} 个建议模型没有注册成功`, { description: modelErrors.join(";") })
    }
    // 建好先测一次:能不能连通、服务商提供哪些模型,右栏立刻看得到
    void queryClient
      .fetchQuery({
        queryKey: probeKey(provider.id),
        queryFn: () => adminApi.testProvider(provider.id),
        retry: false,
        staleTime: 0,
        gcTime: 30 * 60_000,
      })
      .catch(() => undefined)
    setQ("")
    setSelectedParam(provider.id)
  }

  function handleModelSaved(model: ModelEntry, created: boolean) {
    if (!created) {
      toast.success(`已保存 ${model.id}`)
      return
    }
    toast.success(`已注册模型 ${model.id}`, {
      description: `${model.provider_name} · ${model.model}`,
      action: { label: "测试", onClick: () => openTest(model) },
    })
    setSelectedParam(model.provider_id)
  }

  const loading = providersQuery.isLoading || modelsQuery.isLoading
  const loadError = providersQuery.error ?? modelsQuery.error
  const openNewProvider = () => {
    setEditingProvider(null)
    setProviderFormOpen(true)
  }

  return (
    <>
      {loading ? (
        <Card className="grid gap-4 p-3 md:grid-cols-[16rem_minmax(0,1fr)]">
          <div className="flex flex-col gap-2">
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-10" />
            ))}
          </div>
          <div className="flex flex-col gap-3">
            <Skeleton className="h-10 w-64" />
            <Skeleton className="h-9" />
            <Skeleton className="h-9" />
            <Skeleton className="h-32" />
          </div>
        </Card>
      ) : loadError ? (
        <ErrorState
          error={loadError}
          onRetry={() => {
            void providersQuery.refetch()
            void modelsQuery.refetch()
          }}
        />
      ) : providers.length === 0 ? (
        <Card>
          <EmptyState
            icon={ServerIcon}
            title="还没有服务商"
            description="从预设新建一家(OpenAI、硅基流动、TypeSafe Jev……),或者把环境变量里的旧配置一键导入。"
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button size="sm" onClick={openNewProvider}>
                  <PlusIcon /> 新建服务商
                </Button>
                <Button size="sm" variant="outline" onClick={() => void handleImport()}>
                  <DatabaseZapIcon /> 从环境变量导入
                </Button>
              </div>
            }
          />
        </Card>
      ) : (
        // 两栏各自滚动,高度跟着窗口:清单长了不把右栏推下去
        <Card className="grid gap-0 overflow-hidden p-0 md:h-[calc(100svh-9.5rem)] md:min-h-[32rem] md:grid-cols-[16rem_minmax(0,1fr)]">
          <div className="min-h-0 border-b md:border-e md:border-b-0">
            <ProviderList
              providers={providers}
              models={models}
              selectedId={selected?.id ?? null}
              query={q}
              onQueryChange={setQ}
              onSelect={(p) => setSelectedParam(p.id)}
              onAdd={openNewProvider}
              onImport={() => void handleImport()}
              onTestAll={() => void handleTestAll()}
              importing={importEnv.isPending}
              testingAll={testingAll}
            />
          </div>
          <ScrollArea className="min-h-0">
            {selected ? (
              <ProviderPane
                key={selected.id}
                provider={selected}
                models={selectedModels}
                providers={providers}
                onAction={handleProviderAction}
                onToggle={(p, enabled) => void handleProviderToggle(p, enabled)}
                onModelAction={handleModelAction}
                onModelToggle={(m, enabled) => void handleModelToggle(m, enabled)}
                onPickProbeModel={(p, name) => openModelForm(null, { providerId: p.id, model: name, kind: guessKind(name, p.protocol) })}
              />
            ) : null}
          </ScrollArea>
        </Card>
      )}

      <ProviderFormSheet
        open={providerFormOpen}
        onOpenChange={(open) => {
          setProviderFormOpen(open)
          if (!open) setEditingProvider(null)
        }}
        provider={editingProvider}
        providers={providers}
        models={models}
        onSaved={handleProviderSaved}
      />
      <ModelFormSheet
        open={modelFormOpen}
        onOpenChange={(open) => {
          setModelFormOpen(open)
          if (!open) {
            setEditingModel(null)
            setModelInitial(null)
          }
        }}
        model={editingModel}
        initial={modelInitial}
        providers={providers}
        models={models}
        onSaved={handleModelSaved}
      />
      <ModelTestDialog model={testingModel} open={testOpen} onOpenChange={setTestOpen} />
      <ProviderDeleteDialog
        provider={deletingProvider}
        models={models}
        open={deleteProviderOpen}
        onOpenChange={setDeleteProviderOpen}
        onDeleted={(provider, removed) =>
          toast.success(`已删除服务商「${provider.name}」`, {
            description: removed.length > 0 ? `一并删除了 ${removed.join("、")}。` : undefined,
          })
        }
      />
      <ModelDeleteDialog
        model={deletingModel}
        open={deleteModelOpen}
        onOpenChange={setDeleteModelOpen}
        onDeleted={(model, unbound) =>
          toast.success(`已删除模型 ${model.id}`, {
            description: unbound.length > 0 ? `已解除:${unbound.map(usageLabel).join("、")}。` : undefined,
          })
        }
      />
      <ImportEnvResultDialog result={importResult} open={importOpen} onOpenChange={setImportOpen} />
      {confirmDialog}
    </>
  )
}
