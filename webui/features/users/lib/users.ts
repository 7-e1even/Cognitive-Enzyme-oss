import { API_BASE } from "@/lib/api/client"
import type { AdminUser, Role } from "@/lib/api/types"

/** 两种角色各能做什么,创建对话框与成员抽屉里讲给管理员听。 */
export const ROLE_OPTIONS: { value: Role; label: string; description: string }[] = [
  {
    value: "user",
    label: "操作员",
    description: "使用酶库、隔离审核与学习页;不能修改模型、运行参数、接入与用户",
  },
  {
    value: "admin",
    label: "管理员",
    description: "全部功能,另外可以配置模型与运行参数、管理用户、查看审计日志",
  },
]

export type RoleFilter = "all" | Role
export type StatusFilter = "all" | "active" | "disabled"

export const ROLE_FILTER_OPTIONS: { value: RoleFilter; label: string }[] = [
  { value: "all", label: "全部角色" },
  { value: "admin", label: "管理员" },
  { value: "user", label: "操作员" },
]

export const STATUS_TABS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "active", label: "启用" },
  { value: "disabled", label: "已停用" },
]

export function isRoleFilter(value: string | null): value is RoleFilter {
  return value === "all" || value === "admin" || value === "user"
}

export function isStatusFilter(value: string | null): value is StatusFilter {
  return value === "all" || value === "active" || value === "disabled"
}

/** 后端给的头像是 /api/avatars/… 的相对路径;配置了直连地址时要拼上。 */
export function avatarSrc(url: string | null | undefined): string | undefined {
  if (!url) return undefined
  return url.startsWith("/") ? `${API_BASE}${url}` : url
}

export function userSearchBlob(user: AdminUser) {
  return `${user.username} ${user.display_name}`.toLowerCase()
}

/**
 * 这个账号为什么不能被降级 / 停用 / 删除。
 *
 * 和后端的两条守卫一字不差:不能对自己做这些事,也不能拿掉最后一个启用的管理员。
 * 界面先把按钮置灰并说明原因;万一两个管理员同时操作,后端的拒绝信息照样会弹出来。
 */
export function protectionReason(
  target: AdminUser,
  meId: string | undefined,
  activeAdmins: number,
): { self: boolean; lastAdmin: boolean; message: string | null } {
  const self = Boolean(meId) && target.id === meId
  const lastAdmin = target.role === "admin" && target.is_active && activeAdmins <= 1
  const message = self
    ? "这是你自己的账号:不能取消自己的管理员身份、停用或删除自己"
    : lastAdmin
      ? "这是最后一个启用的管理员:至少需要保留一个启用的管理员"
      : null
  return { self, lastAdmin, message }
}

// 去掉了容易看错的 0 / O、1 / l / I,口头或截图转交时不会抄错
const LOWER = "abcdefghijkmnopqrstuvwxyz"
const UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
const DIGITS = "23456789"
const SYMBOLS = "-_@#%+"

function pick(alphabet: string, random: number) {
  return alphabet[random % alphabet.length]
}

/** 16 位随机密码:大小写字母、数字、符号各至少一个,用 crypto 取随机数。 */
export function generatePassword(length = 16): string {
  const all = LOWER + UPPER + DIGITS + SYMBOLS
  const random = new Uint32Array(Math.max(length, 4))
  crypto.getRandomValues(random)
  const chars = [
    pick(LOWER, random[0]),
    pick(UPPER, random[1]),
    pick(DIGITS, random[2]),
    pick(SYMBOLS, random[3]),
  ]
  for (let i = 4; i < length; i++) chars.push(pick(all, random[i]))
  // Fisher–Yates,打乱前四个保底字符的位置
  const order = new Uint32Array(chars.length)
  crypto.getRandomValues(order)
  for (let i = chars.length - 1; i > 0; i--) {
    const j = order[i] % (i + 1)
    ;[chars[i], chars[j]] = [chars[j], chars[i]]
  }
  return chars.join("")
}

export const PASSWORD_MIN = 8
