// 运行参数的前端规则:草稿值的形态、与服务端值的比较、提交前的校验与格式化。
// 与 app/settings.py 的 coerce() 保持一致——那边会拒绝的值,这里提前指出来,不必等 422。
import type { SettingItem, SettingValue } from "@/lib/api/types"

/** 表单里的值:数字与 JSON 按用户敲的文本保存,提交时再转换,半截输入不会被吞掉。 */
export type DraftValue = boolean | string | string[]

export function draftFromValue(item: SettingItem, value: SettingValue | null | undefined = item.value): DraftValue {
  switch (item.type) {
    case "bool":
      return Boolean(value)
    case "list":
    case "models":
      return Array.isArray(value) ? value.map(String) : []
    case "json":
      return value === undefined || value === null ? "" : JSON.stringify(value, null, 2)
    default:
      return value === undefined || value === null ? "" : String(value)
  }
}

export function sameDraft(item: SettingItem, a: DraftValue, b: DraftValue): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i])
  }
  if (typeof a === "boolean" || typeof b === "boolean") return a === b
  if (item.type === "int" || item.type === "float") {
    const x = a.trim()
    const y = b.trim()
    return x === y || (x !== "" && y !== "" && Number(x) === Number(y))
  }
  if (item.type === "json") {
    try {
      return JSON.stringify(JSON.parse(a || "{}")) === JSON.stringify(JSON.parse(b || "{}"))
    } catch {
      return a === b
    }
  }
  return a.trim() === b.trim()
}

const fmt = (n: number) => (Number.isInteger(n) ? String(n) : String(n))

/** 提交前的校验;返回 undefined 表示可以提交。 */
export function validateDraft(item: SettingItem, value: DraftValue): string | undefined {
  if (item.type === "int" || item.type === "float") {
    const raw = String(value).trim()
    if (raw === "") return "请填写数值"
    const n = Number(raw)
    if (!Number.isFinite(n)) return "需要是数字"
    if (item.type === "int" && !Number.isInteger(n)) return "需要整数"
    if (item.min !== null && item.min !== undefined && n < item.min) return `不能小于 ${fmt(item.min)}`
    if (item.max !== null && item.max !== undefined && n > item.max) return `不能大于 ${fmt(item.max)}`
    return undefined
  }
  if (item.type === "enum") {
    return item.choices.some((c) => c.value === value) ? undefined : "取值不在可选范围内"
  }
  if (item.type === "json") {
    const raw = String(value).trim()
    if (!raw) return undefined
    try {
      JSON.parse(raw)
      return undefined
    } catch (error) {
      return `JSON 格式错误:${(error as Error).message}`
    }
  }
  return undefined
}

export function toSubmitValue(item: SettingItem, value: DraftValue): SettingValue {
  switch (item.type) {
    case "bool":
      return Boolean(value)
    case "int":
    case "float":
      return Number(String(value).trim())
    case "list":
    case "models":
      return Array.isArray(value) ? value : []
    case "json": {
      const raw = String(value).trim()
      return raw ? (JSON.parse(raw) as Record<string, unknown>) : {}
    }
    default:
      return String(value).trim()
  }
}

/** 给人看的值:默认值、环境变量值、恢复默认后会变成什么。 */
export function formatSettingValue(item: SettingItem, value: SettingValue | null | undefined): string {
  if (value === null || value === undefined) return "—"
  if (item.type === "bool") return value ? "开启" : "关闭"
  if (item.type === "enum") return item.choices.find((c) => c.value === value)?.label ?? String(value)
  if (Array.isArray(value)) return value.length > 0 ? value.join(", ") : "(空)"
  if (typeof value === "object") return JSON.stringify(value)
  if (value === "") return "(空)"
  return `${value}${item.unit ? ` ${item.unit}` : ""}`
}

/** 恢复默认之后生效的值:环境变量优先,其次内置默认。 */
export function fallbackValue(item: SettingItem): { value: SettingValue; from: "env" | "default" } {
  if (item.env_value !== null && item.env_value !== undefined) return { value: item.env_value, from: "env" }
  return { value: item.default, from: "default" }
}

/** 数字输入框的步长:概率类 0.01,秒级 0.1,大数 1。 */
export function stepFor(item: SettingItem): number {
  if (item.type === "int") return 1
  if (item.max !== null && item.max !== undefined && item.max <= 1) return 0.01
  const d = Math.abs(Number(item.default))
  if (d > 0 && d < 1) return 0.01
  if (d < 10) return 0.1
  return 1
}

export function rangeHint(item: SettingItem): string | undefined {
  if (item.type !== "int" && item.type !== "float") return undefined
  const unit = item.unit ? ` ${item.unit}` : ""
  if (item.min !== null && item.max !== null) return `${fmt(item.min)} ~ ${fmt(item.max)}${unit}`
  if (item.min !== null) return `≥ ${fmt(item.min)}${unit}`
  if (item.max !== null) return `≤ ${fmt(item.max)}${unit}`
  return undefined
}

export function matchesSetting(item: SettingItem, needle: string): boolean {
  if (!needle) return true
  return [item.label, item.key, item.env, item.help].some((field) => (field ?? "").toLowerCase().includes(needle))
}
