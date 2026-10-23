/**
 * "刚刚" / "12 分钟前" / "5 小时前" / "3 天前"。
 *
 * 模板里按固定的演示时间计算;这里接的是真实数据,默认用当前时间。
 */
export function relativeTime(iso: string, now: Date = new Date()) {
  const deltaMs = now.getTime() - new Date(iso).getTime()
  const minutes = Math.round(deltaMs / 60_000)

  if (minutes < 1) return "刚刚"
  if (minutes < 60) return `${minutes} 分钟前`

  const hours = Math.round(minutes / 60)

  if (hours < 24) return `${hours} 小时前`

  return `${Math.round(hours / 24)} 天前`
}
