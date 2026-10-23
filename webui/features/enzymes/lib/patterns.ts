// 触发条件(关键词 / 正则)的解析与校验。
import type { PatternItem } from "@/lib/api/types"

/**
 * 用浏览器的正则引擎近似检查 Python `re` 语法。
 * Python 专有的命名分组写法先换成 JS 等价写法;内联标志 `(?i)` 只能出现在开头,JS 不支持,去掉再试。
 */
export function regexError(source: string): string | undefined {
  const translated = source
    .replace(/\(\?P<(\w+)>/g, "(?<$1>")
    .replace(/\(\?P=(\w+)\)/g, "\\k<$1>")
    .replace(/^\(\?[aiLmsux]+\)/, "")
  try {
    new RegExp(translated, "u")
    return undefined
  } catch {
    try {
      new RegExp(translated)
      return undefined
    } catch (error) {
      const message = error instanceof Error ? error.message.replace(/^Invalid regular expression: /, "") : ""
      return `正则表达式无效${message ? `:${message}` : ""}`
    }
  }
}

/**
 * 把输入框里的一段文字读成一条触发条件。
 * `regex:` 前缀或 `/…/` 包裹的按正则处理,其余按当前选中的类型。
 */
export function readPattern(text: string, fallbackType: PatternItem["type"]): PatternItem | null {
  const value = text.trim()
  if (!value) return null
  if (value.toLowerCase().startsWith("regex:")) {
    const source = value.slice(6).trim()
    return source ? { type: "regex", value: source } : null
  }
  if (value.length > 2 && value.startsWith("/") && value.endsWith("/")) {
    return { type: "regex", value: value.slice(1, -1) }
  }
  return { type: fallbackType, value }
}

export function samePattern(a: PatternItem, b: PatternItem) {
  return a.type === b.type && (a.type === "regex" ? a.value === b.value : a.value.toLowerCase() === b.value.toLowerCase())
}

/** 列表里一行的触发条件:关键词原样,正则用 /…/ 标出。 */
export function patternText(item: PatternItem) {
  return item.type === "regex" ? `/${item.value}/` : item.value
}

/**
 * 列表接口只给 `patterns_readable`:" / " 连接,正则带「正则:」前缀。拆回条目用于表格展示;
 * 正则本身含 " / " 时会拆偏,编辑一律以详情接口的 pattern_list 为准。
 */
export function splitReadablePatterns(readable: string): PatternItem[] {
  return readable
    .split(" / ")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) =>
      part.startsWith("正则:") ? { type: "regex" as const, value: part.slice(3) } : { type: "keyword" as const, value: part },
    )
}
