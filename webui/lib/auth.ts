// 登录态:JWT 存 localStorage,请求由 lib/api/client.ts 自动附带 Authorization 头。
// 这里只管存取,不发请求(避免与 client 循环依赖);登录 / 资料接口在 lib/api/endpoints.ts。
//
// 另有一个不含任何秘密的 `ce_session=1` cookie:服务端的 proxy.ts 看不到 localStorage,
// 只能凭它判断"这个浏览器声称已登录",没有它就直接送去登录页,省掉一次先渲染外壳再跳走的闪烁。
// cookie 只是提示,真正的校验仍是带令牌请求 /api/auth/me;cookie 过期 / 残留都不会造成跳转死循环,
// 因为登录页自己会验证令牌(见 features/auth)。

const TOKEN_KEY = "ce_token"

/** proxy.ts 读取的会话提示 cookie 名。 */
export const SESSION_COOKIE = "ce_session"

export function getToken(): string | null {
  if (typeof window === "undefined") return null
  try {
    return window.localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

/** 写入会话提示 cookie。令牌有效但 cookie 丢了(浏览器关闭即清除)时,也要补写,否则 proxy 会一直送去登录页。 */
export function setSessionCookie(): void {
  if (typeof document === "undefined") return
  document.cookie = `${SESSION_COOKIE}=1; Path=/; SameSite=Lax`
}

function clearSessionCookie(): void {
  if (typeof document === "undefined") return
  document.cookie = `${SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax`
}

export function hasSessionCookie(): boolean {
  if (typeof document === "undefined") return false
  return document.cookie.split(";").some((part) => part.trim().startsWith(`${SESSION_COOKIE}=`))
}

export function setToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* 隐私模式等存储不可用:本次会话内失效即可 */
  }
  setSessionCookie()
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
  clearSessionCookie()
}

export function logout(): void {
  clearSession()
  if (typeof window !== "undefined") window.location.href = "/sign-in"
}
