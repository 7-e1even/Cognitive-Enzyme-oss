/**
 * 路由 → 标题,以及路由在侧栏里所属的分组。
 *
 * 标题与侧栏导航的文字一致,面包屑读起来就和点过的那一项相同。放在这里而不是从导航数据推导:
 * 导航数据带 JSX 图标,页面 metadata 不该为了取个名字把图标拉进依赖图。
 */

export const PRODUCT_NAME = "Cognitive Enzyme"

const ROUTE_TITLES: Record<string, string> = {
  "/": "首页",
  "/flow": "工作流程",
  "/enzymes": "记忆",
  "/review": "隔离审核",
  "/playground": "召回调试",
  "/learning": "巩固调度",
  "/learning/consolidation": "巩固调度",
  "/learning/metrics": "详细指标",
  "/learning/evolution": "进化事件",
  "/learning/gardener": "夜间园丁",
  "/learning/distill": "判定记录",
  "/models": "服务商与模型",
  "/models/providers": "服务商与模型",
  "/models/bindings": "模型绑定",
  "/models/gateway": "网关路由",
  "/settings": "运行参数",
  "/settings/runtime": "运行参数",
  "/settings/security": "访问与安全",
  "/settings/integrations": "接入",
  "/admin": "用户",
  "/admin/users": "用户",
  "/admin/audit": "审计日志",
  "/admin/jobs": "后台任务",
  "/admin/system": "系统状态",
}

/** 子页面所属的分组(侧栏里可折叠的那一层);分组本身没有页面,访问时重定向到第一个子页。 */
const ROUTE_SECTIONS: Record<string, string> = {
  "/learning/consolidation": "学习",
  "/learning/metrics": "学习",
  "/learning/evolution": "学习",
  "/learning/gardener": "学习",
  "/learning/distill": "学习",
  "/models/providers": "模型",
  "/models/bindings": "模型",
  "/models/gateway": "模型",
  "/settings/runtime": "设置",
  "/settings/security": "设置",
  "/settings/integrations": "设置",
  "/admin/users": "运维",
  "/admin/audit": "运维",
  "/admin/jobs": "运维",
  "/admin/system": "运维",
}

/** 表里 pathname 所在的最长祖先路由。 */
function ancestorOf(pathname: string): string | undefined {
  return Object.keys(ROUTE_TITLES)
    .filter((route) => route !== "/" && pathname.startsWith(`${route}/`))
    .sort((a, b) => b.length - a.length)[0]
}

/** pathname 的标题;详情类子路由回退到最长祖先的标题。 */
export function routeTitle(pathname: string): string {
  const exact = ROUTE_TITLES[pathname]
  if (exact) return exact
  const ancestor = ancestorOf(pathname)
  return ancestor ? ROUTE_TITLES[ancestor] : PRODUCT_NAME
}

/** 面包屑:分组在前,如 `/learning/distill` → ["学习", "判定记录"]。 */
export function routeTrail(pathname: string): string[] {
  const key = ROUTE_TITLES[pathname] ? pathname : ancestorOf(pathname)
  if (!key) return [routeTitle(pathname)]
  const section = ROUTE_SECTIONS[key]
  return section ? [section, ROUTE_TITLES[key]] : [ROUTE_TITLES[key]]
}

/** 页面 metadata 标题,如 "记忆 · Cognitive Enzyme"。 */
export function pageTitle(title: string) {
  return `${title} · ${PRODUCT_NAME}`
}

// ── 页面宽度 ────────────────────────────────────────────────

/**
 * 页面宽度按类型分两种,和 roster-hr 模板的 AppLayout 一个思路:
 *
 * - 铺满(默认):表格、看板、画布这类数据页,照 flux 模板整行铺满——flux 的总览和审批页
 *   本来是区块的 1320px 居中,模板特意放开成铺满;tempo 模板也是全部铺满。
 * - 限宽居中:设置和表单类页面。铺满时名称和控件隔得太远、说明文字一行太长,读起来费劲;
 *   roster 的设置页收在 4xl,这里多给一档(5xl / 1024px),运行参数页左边还有一列分组导航。
 *
 * 顶栏不跟着收:照模板整条铺满,面包屑在左上。
 */
const NARROW_ROUTES = [
  "/settings/runtime",
  "/settings/security",
  "/settings/integrations",
  "/models/bindings",
  "/models/gateway",
  "/learning/consolidation",
  "/learning/evolution",
]

export function pageContainer(pathname: string) {
  const narrow = NARROW_ROUTES.some((route) => pathname === route || pathname.startsWith(`${route}/`))
  return narrow ? "mx-auto w-full max-w-5xl" : "w-full"
}
