import { NextResponse, type NextRequest } from "next/server"

import { SESSION_COOKIE } from "@/lib/auth"

/**
 * Next 16 把 `middleware.ts` 改名为 `proxy.ts`,导出名是 `proxy`。
 *
 * 控制台需要登录:没有会话提示 cookie 的请求在服务端就送去登录页,并把原地址带在 `?next=` 里,
 * 登录后原路返回。cookie 不含令牌,只说明"这个浏览器登录过";令牌是否有效由客户端的
 * 登录守卫(features/shell)与登录页(features/auth)各自核验。
 *
 * 反方向(已登录却打开 /sign-in)这里故意不管:服务端判断不了 cookie 背后的令牌还有没有效,
 * 如果把带着过期 cookie 的人从登录页送回控制台,守卫又会把他送回来,形成死循环。
 * 登录页自己验证令牌,有效才跳走。
 */
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl

  if (pathname === "/sign-in" || pathname.startsWith("/sign-in/")) {
    return NextResponse.next()
  }

  if (request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.next()
  }

  // 客户端路由切换时的 RSC 请求带 `_rsc` 参数,它不属于用户看到的地址,不该被带回来。
  const params = new URLSearchParams(request.nextUrl.search)
  params.delete("_rsc")
  const query = params.toString()
  const back = `${pathname}${query ? `?${query}` : ""}`

  const url = request.nextUrl.clone()
  url.pathname = "/sign-in"
  url.search = back === "/" ? "" : `?next=${encodeURIComponent(back)}`

  return NextResponse.redirect(url)
}

export const config = {
  // 除了后端接口(/api/*,由 next.config 转发)、Next 自身资源与静态文件,其余路径都要过这道门。
  matcher: [
    "/((?!api/|api$|_next/static|_next/image|favicon.ico|.*\\.(?:png|jpg|jpeg|gif|webp|svg|ico|txt|xml|webmanifest)$).*)",
  ],
}
