import {
  Activity, BookOpen, Brain, Bug, ClipboardCheck, Cpu, Database, GitBranch, History, KeyRound, ListChecks,
  Network, Plug, Search, Server, Settings2, ShieldCheck, Sprout, Users, Workflow, type LucideIcon,
} from "lucide-react"

export interface NavItem {
  title: string
  href: string
  icon: LucideIcon
  adminOnly?: boolean
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

/** Same information architecture as the hosted console: memories first, learning, models, settings, ops. */
export const NAV: NavGroup[] = [
  {
    label: "工作区",
    items: [
      { title: "首页", href: "/", icon: Activity },
      { title: "工作流程", href: "/flow", icon: Workflow },
      { title: "记忆", href: "/enzymes", icon: Database },
      { title: "隔离审核", href: "/review", icon: ClipboardCheck },
      { title: "召回调试", href: "/playground", icon: Search },
    ],
  },
  {
    label: "学习",
    items: [
      { title: "巩固调度", href: "/learning/consolidation", icon: Brain },
      { title: "详细指标", href: "/learning/metrics", icon: ListChecks },
      { title: "进化事件", href: "/learning/evolution", icon: GitBranch },
      { title: "夜间园丁", href: "/learning/gardener", icon: Sprout },
      { title: "判定记录", href: "/learning/distill", icon: History },
    ],
  },
  {
    label: "模型",
    items: [
      { title: "服务商与模型", href: "/models/providers", icon: Cpu },
      { title: "模型绑定", href: "/models/bindings", icon: Network },
      { title: "网关路由", href: "/models/gateway", icon: Plug },
    ],
  },
  {
    label: "设置",
    items: [
      { title: "运行参数", href: "/settings/runtime", icon: Settings2 },
      { title: "访问与安全", href: "/settings/security", icon: ShieldCheck },
      { title: "接入", href: "/settings/integrations", icon: BookOpen },
    ],
  },
  {
    label: "运维",
    items: [
      { title: "用户", href: "/admin/users", icon: Users, adminOnly: true },
      { title: "审计日志", href: "/admin/audit", icon: KeyRound, adminOnly: true },
      { title: "后台任务", href: "/admin/jobs", icon: Bug, adminOnly: true },
      { title: "系统状态", href: "/admin/system", icon: Server, adminOnly: true },
    ],
  },
]
