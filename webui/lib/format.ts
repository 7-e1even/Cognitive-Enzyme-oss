// 数值与时间的统一格式(zh-CN)。

const nf = new Intl.NumberFormat("zh-CN")
const pf = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 })
const rtf = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" })
const dtf = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
})

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—"
  return nf.format(n)
}

export function formatPercent(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—"
  return pf.format(n)
}

export function formatProb(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—"
  return n.toFixed(digits)
}

export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${(bytes / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? "—" : dtf.format(d)
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—"
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return "—"
  const diff = (t - Date.now()) / 1000
  const abs = Math.abs(diff)
  if (abs < 45) return "刚刚"
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute")
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour")
  if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day")
  return formatDateTime(iso)
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—"
  const s = Math.round(seconds)
  if (s < 60) return `${s} 秒`
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`
  return `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时`
}

export function formatInterval(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`
  return `${seconds} 秒`
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return "—"
  return id.length > n ? id.slice(0, n) : id
}

export function initials(name: string): string {
  const s = (name || "?").trim()
  return /[一-鿿]/.test(s[0] ?? "") ? s.slice(0, 1) : s.slice(0, 2).toUpperCase()
}

/** "9/25" —— 图表横轴与表格用的短日期(入参是 "2026-09-25" 这样的日期)。 */
export function shortDay(day: string) {
  const [, month, date] = day.split("-")
  return `${Number(month)}/${Number(date)}`
}

/** "9月25日 周四" —— 悬浮提示里的完整日期。 */
export function longDay(day: string) {
  const d = new Date(`${day}T00:00:00`)
  if (Number.isNaN(d.getTime())) return day
  return new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(d)
}
