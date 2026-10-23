"use client"

import { toast } from "sonner"

/** 把数据存成一个 JSON 文件。链接先挂进文档再点击(Firefox 不下载游离的链接),对象 URL 下一拍再回收。 */
export function saveJson(filename: string, value: unknown, what: string) {
  if (typeof document === "undefined" || typeof URL.createObjectURL !== "function") {
    toast.error(`没能导出${what}`, { description: "这个浏览器不允许页面写文件" })
    return
  }
  const url = URL.createObjectURL(new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: "application/json" }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
  toast.success(`已导出${what}`, { description: filename })
}

/** 文件名里的时间戳:2026-09-25-1116 */
export function fileStamp(date = new Date()) {
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`
}
