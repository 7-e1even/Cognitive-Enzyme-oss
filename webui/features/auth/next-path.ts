/**
 * 登录后回到哪里。`?next=` 来自地址栏,谁都能改,所以只接受本站的路径:
 * 以单个 "/" 开头(排除 "//evil.com" 这种协议相对地址和 "/\\evil.com"),
 * 解析后仍在同一个源上,且不是登录页自己(否则会原地打转)。其余一律回总览。
 */
const BASE = "http://ce.invalid"

export function safeNextPath(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/"

  try {
    const url = new URL(raw, BASE)
    if (url.origin !== BASE) return "/"
    if (url.pathname === "/sign-in" || url.pathname.startsWith("/sign-in/")) return "/"
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return "/"
  }
}
