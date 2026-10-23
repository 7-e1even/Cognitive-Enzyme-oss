// 新建服务商时的预设:协议 + 接口地址 + 建议模型。模型名只写确定存在的(或仓库示例里用过的),
// 其余靠「测试连接」拉回的模型列表挑选,不替服务商编型号。
import type { ModelKind, ModelParams, Protocol } from "@/lib/api/types"

export interface PresetModel {
  /** 建议的注册名;与已有模型重名时自动加后缀 */
  id: string
  kind: ModelKind
  model: string
  params?: ModelParams
  /** 默认勾选:这类服务商几乎总是为它而接入的 */
  checked?: boolean
}

export interface PresetVariant {
  key: string
  label: string
  protocol: Protocol
  baseUrl: string
  hint?: string
}

export interface ProviderPreset {
  key: string
  name: string
  /** 建议的服务商 ID */
  id: string
  description: string
  variants: PresetVariant[]
  models?: PresetModel[]
  headers?: Record<string, string>
  /** 本地服务通常不需要密钥 */
  keyOptional?: boolean
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    key: "openai",
    name: "OpenAI",
    id: "openai",
    description: "Chat Completions 与 Embeddings",
    variants: [{ key: "default", label: "官方", protocol: "openai", baseUrl: "https://api.openai.com/v1" }],
    models: [
      { id: "text-embedding-3-small", kind: "embedding", model: "text-embedding-3-small", params: { dimensions: 1536 } },
    ],
  },
  {
    key: "anthropic",
    name: "Anthropic",
    id: "anthropic",
    description: "Claude,Messages API",
    variants: [{ key: "default", label: "官方", protocol: "anthropic", baseUrl: "https://api.anthropic.com" }],
    models: [{ id: "claude-sonnet-5", kind: "chat", model: "claude-sonnet-5", params: { max_tokens: 16000 } }],
  },
  {
    key: "siliconflow",
    name: "硅基流动 SiliconFlow",
    id: "siliconflow",
    description: "开源向量 / 精排模型,记忆召回的默认搭配",
    variants: [{ key: "default", label: "官方", protocol: "openai", baseUrl: "https://api.siliconflow.cn/v1" }],
    models: [
      { id: "bge-m3", kind: "embedding", model: "BAAI/bge-m3", params: { dimensions: 1024 }, checked: true },
      { id: "bge-reranker-v2-m3", kind: "rerank", model: "BAAI/bge-reranker-v2-m3", checked: true },
    ],
  },
  {
    key: "openrouter",
    name: "OpenRouter",
    id: "openrouter",
    description: "多家模型的聚合入口",
    variants: [{ key: "default", label: "官方", protocol: "openai", baseUrl: "https://openrouter.ai/api/v1" }],
    headers: { "X-Title": "cognitive-enzyme" },
  },
  {
    key: "deepseek",
    name: "DeepSeek",
    id: "deepseek",
    description: "OpenAI 兼容接口",
    variants: [{ key: "default", label: "官方", protocol: "openai", baseUrl: "https://api.deepseek.com/v1" }],
    models: [{ id: "deepseek-chat", kind: "chat", model: "deepseek-chat" }],
  },
  {
    key: "zhipu",
    name: "智谱 GLM",
    id: "zhipu",
    description: "标准接口、Coding Plan 与 Anthropic 兼容接口",
    variants: [
      { key: "standard", label: "标准", protocol: "openai", baseUrl: "https://open.bigmodel.cn/api/paas/v4" },
      {
        key: "coding",
        label: "Coding Plan",
        protocol: "openai",
        baseUrl: "https://open.bigmodel.cn/api/coding/paas/v4",
        hint: "编程套餐的专用地址,按套餐计费",
      },
      {
        key: "anthropic",
        label: "Anthropic 兼容",
        protocol: "anthropic",
        baseUrl: "https://open.bigmodel.cn/api/anthropic",
      },
    ],
    models: [{ id: "glm-5.3", kind: "chat", model: "glm-5.3" }],
  },
  {
    key: "moonshot",
    name: "月之暗面 Kimi",
    id: "kimi",
    description: "OpenAI 与 Anthropic 两种兼容接口",
    variants: [
      { key: "openai", label: "OpenAI 兼容", protocol: "openai", baseUrl: "https://api.moonshot.cn/v1" },
      { key: "anthropic", label: "Anthropic 兼容", protocol: "anthropic", baseUrl: "https://api.moonshot.cn/anthropic" },
    ],
  },
  {
    key: "dashscope",
    name: "阿里云百炼 DashScope",
    id: "dashscope",
    description: "通义千问,OpenAI 兼容模式",
    variants: [
      {
        key: "default",
        label: "兼容模式",
        protocol: "openai",
        baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      },
    ],
  },
  {
    key: "minimax",
    name: "MiniMax",
    id: "minimax",
    description: "Anthropic 兼容接口",
    variants: [{ key: "default", label: "Anthropic 兼容", protocol: "anthropic", baseUrl: "https://api.minimaxi.com/anthropic" }],
  },
  {
    key: "ollama",
    name: "Ollama",
    id: "ollama",
    description: "本机模型,无需密钥",
    variants: [{ key: "default", label: "本机", protocol: "openai", baseUrl: "http://localhost:11434/v1" }],
    keyOptional: true,
  },
  {
    key: "custom-openai",
    name: "vLLM / 自定义",
    id: "custom",
    description: "任意 OpenAI 兼容服务",
    variants: [{ key: "default", label: "OpenAI 兼容", protocol: "openai", baseUrl: "" }],
    keyOptional: true,
  },
  {
    key: "typesafe",
    name: "TypeSafe Jev",
    id: "typesafe",
    description: "System One 判官协议,适用性判定",
    variants: [{ key: "default", label: "官方", protocol: "system_one", baseUrl: "https://api.typesafe.ai/v1" }],
    models: [{ id: "jev-1.13.0", kind: "judge", model: "jev-1.13.0", checked: true }],
  },
  {
    key: "laya",
    name: "laya-serve 本地",
    id: "laya-local",
    description: "自训判官,System One 协议",
    variants: [{ key: "default", label: "本机", protocol: "system_one", baseUrl: "http://127.0.0.1:8000/v1" }],
    models: [
      { id: "laya", kind: "judge", model: "laya", params: { timeout: 30, concurrency: 8 }, checked: true },
    ],
    keyOptional: true,
  },
]

export function presetByKey(key: string | null | undefined): ProviderPreset | undefined {
  return PROVIDER_PRESETS.find((preset) => preset.key === key)
}
