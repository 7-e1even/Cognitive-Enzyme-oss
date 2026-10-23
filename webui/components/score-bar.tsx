import { Progress } from "@/components/ui/progress"
import { cn } from "@/lib/utils"

/** 0~1 的分数:一条细进度条加两位小数。 */
export function ScoreBar({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <span className="text-xs text-muted-foreground">—</span>
  }
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)
  return (
    <span className={cn("flex items-center gap-2", className)}>
      <Progress value={pct} className="h-1.5 w-14" aria-label="分数" />
      <span className="w-9 text-right text-xs tabular-nums">{value.toFixed(2)}</span>
    </span>
  )
}
