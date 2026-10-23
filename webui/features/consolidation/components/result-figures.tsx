import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { formatNumber } from "@/lib/format"
import { cn } from "@/lib/utils"

export interface ResultFigure {
  key: string
  label: string
  hint?: string
  value: unknown
}

function display(value: unknown) {
  if (typeof value === "number") return formatNumber(value)
  if (typeof value === "boolean") return value ? "是" : "否"
  if (value === null || value === undefined || value === "") return "—"
  if (typeof value === "string") return value
  return JSON.stringify(value)
}

/** 一排带标签的数字:数字在上、名字在下,零值淡一些。不认得的键原样显示成等宽字,不会被悄悄丢掉。 */
export function ResultFigures({ figures, className }: { figures: ResultFigure[]; className?: string }) {
  return (
    <div className={cn("grid grid-cols-[repeat(auto-fill,minmax(7.5rem,1fr))] gap-2", className)}>
      {figures.map((figure) => {
        const body = (
          <div className={cn("flex min-w-0 flex-col gap-0.5 rounded-lg border bg-background px-3 py-2", figure.hint && "cursor-help")}>
            <span className={cn("truncate text-base leading-tight font-semibold tabular-nums", figure.value === 0 && "text-muted-foreground")}>
              {display(figure.value)}
            </span>
            <span className={cn("truncate text-xs text-muted-foreground", !figure.hint && figure.label === figure.key && "font-mono")}>{figure.label}</span>
          </div>
        )
        return figure.hint ? (
          <Tooltip key={figure.key}>
            <TooltipTrigger asChild>
              <div className="min-w-0">{body}</div>
            </TooltipTrigger>
            <TooltipContent>{figure.hint}</TooltipContent>
          </Tooltip>
        ) : (
          <div key={figure.key} className="min-w-0">
            {body}
          </div>
        )
      })}
    </div>
  )
}
