// 后端请求的唯一出口:同源相对路径(next.config.mjs 把 /api/* 代理到后端),
// 需要浏览器直连后端时设置 NEXT_PUBLIC_API_URL。
import { clearSession, getToken } from "@/lib/auth"

export const API_BASE = process.env.NEXT_PUBLIC_API_URL || ""

export class ApiError extends Error {
  status: number
  detail: unknown
  /** 后端校验错误里指明的字段(运行参数的 key 等) */
  field?: string

  constructor(status: number, message: string, detail?: unknown, field?: string) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
    this.field = field
  }
}

type Query = Record<string, string | number | boolean | undefined | null>

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
  body?: unknown
  query?: Query
  signal?: AbortSignal
  /** 401 时不跳登录页(登录页自己的请求) */
  noRedirect?: boolean
}

function buildUrl(path: string, query?: Query) {
  const url = `${API_BASE}${path}`
  if (!query) return url
  const qs = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue
    qs.set(key, String(value))
  }
  const s = qs.toString()
  return s ? `${url}?${s}` : url
}

function parseDetail(detail: unknown): { message: string; field?: string } {
  if (typeof detail === "string") return { message: detail }
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>
    if (typeof d.message === "string") {
      return { message: d.message, field: typeof d.key === "string" ? d.key : undefined }
    }
    if (Array.isArray(detail)) {
      // FastAPI 422:[{loc, msg}]
      const first = detail[0] as { loc?: unknown[]; msg?: string } | undefined
      if (first?.msg) {
        const loc = Array.isArray(first.loc) ? first.loc.slice(1).join(".") : ""
        return { message: loc ? `${loc}: ${first.msg}` : first.msg }
      }
    }
  }
  return { message: "请求失败" }
}

function redirectToLogin() {
  if (typeof window === "undefined") return
  if (window.location.pathname.startsWith("/sign-in")) return
  const next = encodeURIComponent(window.location.pathname + window.location.search)
  window.location.href = `/sign-in?next=${next}`
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const token = getToken()
  const isForm = typeof FormData !== "undefined" && options.body instanceof FormData
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined && !isForm) headers["Content-Type"] = "application/json"

  let res: Response
  try {
    res = await fetch(buildUrl(path, options.query), {
      method: options.method ?? (options.body !== undefined ? "POST" : "GET"),
      headers,
      body:
        options.body === undefined
          ? undefined
          : isForm
            ? (options.body as FormData)
            : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e
    throw new ApiError(0, "无法连接后端服务,请确认 API 已启动")
  }

  if (res.status === 401 && !options.noRedirect) {
    clearSession()
    redirectToLogin()
    throw new ApiError(401, "登录已过期,请重新登录")
  }

  const text = await res.text()
  let data: unknown = undefined
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | undefined)?.detail ?? data
    const { message, field } = parseDetail(detail)
    throw new ApiError(res.status, message || `请求失败 (${res.status})`, detail, field)
  }
  return data as T
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) =>
    request<T>(path, { method: "POST", body: body ?? {}, query }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: "PUT", body: body ?? {} }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: "PATCH", body: body ?? {} }),
  delete: <T>(path: string, query?: Query) => request<T>(path, { method: "DELETE", query }),
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "未知错误"
}
