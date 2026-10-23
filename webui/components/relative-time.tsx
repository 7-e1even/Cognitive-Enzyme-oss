import { formatDateTime, formatRelative } from "@/lib/format"

/** "3 小时前",悬浮看完整时间。 */
export function RelativeTime({ value, className }: { value: string | null | undefined; className?: string }) {
  if (!value) return <span className={className}>—</span>
  return (
    <time dateTime={value} title={formatDateTime(value)} className={className}>
      {formatRelative(value)}
    </time>
  )
}
