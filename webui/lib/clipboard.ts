"use client"

import { toast } from "sonner"

/**
 * 复制并说一声,两个分支都开口:菜单项一点就关,没有提示的复制和一行坏掉的菜单分不出来;
 * 非安全上下文或被拒绝时 writeText 会失败,也要说。
 */
export async function copyText(value: string, label: string, description?: string): Promise<boolean> {
  if (typeof navigator === "undefined" || !navigator.clipboard) {
    toast.error(`无法复制${label}`, { description: "浏览器没有把剪贴板交给这个页面(需要 HTTPS 或 localhost)。" })
    return false
  }
  try {
    await navigator.clipboard.writeText(value)
    toast.success(`已复制${label}`, { description: description ?? value })
    return true
  } catch {
    toast.error(`无法复制${label}`, { description: "浏览器拒绝了剪贴板访问。" })
    return false
  }
}
