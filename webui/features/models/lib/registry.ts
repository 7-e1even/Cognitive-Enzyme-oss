// 模型注册表在前端的规则:协议 ↔ 类型、注册名格式、引用关系的中文名与参数摘要。
// 与 app/model_registry.py 保持一致——那边拒绝的东西,这里提前说清楚,不等 422。
import type { ModelEntry, ModelKind, ModelParams, Protocol, Provider } from "@/lib/api/types"

export const PROTOCOLS: Protocol[] = ["openai", "anthropic", "system_one"]
export const KINDS: ModelKind[] = ["chat", "embedding", "rerank", "judge"]

export const KIND_PROTOCOLS: Record<ModelKind, Protocol[]> = {
  chat: ["openai", "anthropic"],
  embedding: ["openai"],
  rerank: ["openai"],
  judge: ["system_one"],
}

/** 协议下能建的模型类型,顺序即表单里的顺序。 */
export function kindsFor(protocol: Protocol | "" | undefined): ModelKind[] {
  if (!protocol) return []
  return KINDS.filter((kind) => KIND_PROTOCOLS[kind].includes(protocol))
}

/** 「jev」永远指当前绑定的适用性判官:网关路由与影子判官里可以直接写它。 */
export const JUDGE_ALIAS = "jev"

export const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/
export const ID_RULE = "只能包含字母、数字和 . _ : -,以字母或数字开头,最长 64 位"

/** 模型被谁引用:角色绑定(model.*)或列表参数(网关路由、影子判官)。 */
const USAGE_LABELS: Record<string, string> = {
  "model.llm": "引擎 LLM",
  "model.embedding": "向量模型",
  "model.rerank": "精排模型",
  "model.judge": "适用性判官",
  "model.query_expand": "查询扩写",
  "model.judge_llm": "第二判官",
  "distill.shadow_judges": "影子判官",
  "gateway.route": "网关路由",
}

export function usageLabel(key: string): string {
  return USAGE_LABELS[key] ?? key
}

/** 引用关系在哪一页改:角色去「模型绑定」,列表参数去「网关路由」。 */
export function usageHref(key: string): string {
  return key.startsWith("model.") ? "/models/bindings" : "/models/gateway"
}

export const KIND_TEST_HINT: Record<ModelKind, { label: string; placeholder: string; explain: string }> = {
  chat: {
    label: "提示词",
    placeholder: "用一句话介绍你自己。",
    explain: "发一条用户消息,返回模型的回复。",
  },
  embedding: {
    label: "输入文本",
    placeholder: "connectivity test",
    explain: "请求一次 /embeddings,核对返回的向量维度与配置是否一致。",
  },
  rerank: {
    label: "查询",
    placeholder: "如何避免 SQL 注入?",
    explain: "给一段相关、一段无关的文本打分:相关的应明显更高。",
  },
  judge: {
    label: "判定问题",
    placeholder: "state 描述的是一个编程问题吗?",
    explain: "用固定情境「Python 读取 CSV 报 UnicodeDecodeError」问一道是否题,返回判定概率。",
  },
}

function trimNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)))
}

/** 参数的一行摘要,列表里用;空参数说「默认参数」而不是留白。 */
export function paramsSummary(kind: ModelKind, params: ModelParams): string[] {
  const out: string[] = []
  if (kind === "chat") {
    if ("temperature" in params) {
      out.push(params.temperature === null || params.temperature === undefined ? "不传温度" : `温度 ${trimNumber(params.temperature)}`)
    }
    if (params.max_tokens) out.push(`输出上限 ${params.max_tokens}`)
    if (params.api_version) out.push(`API ${params.api_version}`)
  }
  if (kind === "embedding" && params.dimensions) out.push(`${params.dimensions} 维`)
  if (kind === "rerank" && params.top_n) out.push(`top_n ${params.top_n}`)
  if (kind === "chat" || kind === "judge") {
    if (params.timeout) out.push(`超时 ${trimNumber(params.timeout)} 秒`)
    if (params.concurrency) out.push(`并发 ${params.concurrency}`)
    if (params.retries !== undefined && params.retries !== null) out.push(`重试 ${params.retries}`)
  }
  if (kind === "chat" && params.extra && Object.keys(params.extra).length > 0) {
    out.push(`extra: ${Object.keys(params.extra).join(", ")}`)
  }
  return out
}

/** 与后端 _slug 同一规则:服务商 / 模型 ID 的建议值。 */
export function slugify(text: string, fallback = "model"): string {
  const s = (text || "")
    .replace(/[^A-Za-z0-9._:-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "")
  return (s || fallback).slice(0, 64)
}

/** 已被占用时加 -2、-3……与后端导入时的命名方式一致。 */
export function uniqueId(base: string, taken: Iterable<string>): string {
  const used = new Set(taken)
  if (!used.has(base)) return base
  for (let n = 2; n < 1000; n += 1) {
    const candidate = `${base.slice(0, 60)}-${n}`
    if (!used.has(candidate)) return candidate
  }
  return base
}

/** 服务侧模型名里看得出来的类型;猜不出就当对话模型。 */
export function guessKind(modelName: string, protocol: Protocol): ModelKind {
  const allowed = kindsFor(protocol)
  const name = modelName.toLowerCase()
  const guess: ModelKind = name.includes("rerank")
    ? "rerank"
    : name.includes("embed") || name.includes("bge-m3") || name.includes("bge-large")
      ? "embedding"
      : protocol === "system_one"
        ? "judge"
        : "chat"
  return allowed.includes(guess) ? guess : (allowed[0] ?? "chat")
}

/** 模型可用 = 自己启用 + 服务商启用 + 协议匹配;网关与角色解析都按这个标准跳过。 */
export function modelUsable(model: ModelEntry, providers: Provider[] | undefined): boolean {
  if (!model.enabled) return false
  const provider = providers?.find((p) => p.id === model.provider_id)
  if (!provider) return !providers
  return provider.enabled && KIND_PROTOCOLS[model.kind].includes(provider.protocol)
}

/** 为什么不可用,说给人听;可用时返回 undefined。 */
export function unusableReason(model: ModelEntry | undefined, providers: Provider[] | undefined): string | undefined {
  if (!model) return "模型不存在"
  if (!model.enabled) return "模型已停用"
  const provider = providers?.find((p) => p.id === model.provider_id)
  if (providers && !provider) return "服务商不存在"
  if (provider && !provider.enabled) return `服务商「${provider.name}」已停用`
  if (provider && !KIND_PROTOCOLS[model.kind].includes(provider.protocol)) return "服务商协议与模型类型不符"
  return undefined
}

/** 服务商名的两字母徽标:图标块里用,中文名取首字。 */
export function monogram(name: string): string {
  const s = (name || "?").trim()
  if (/^[一-鿿]/.test(s)) return s.slice(0, 1)
  const words = s.split(/[\s\-_/.]+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return s.slice(0, 2).toUpperCase()
}
