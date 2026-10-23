import { Button } from "@/components/ui/button"
import { formatNumber } from "@/lib/format"

/** 表格下方的翻页条:只有一页时不出现。`page` 从 0 开始。 */
export function Pager({
  page,
  pageSize,
  total,
  onPageChange,
}: {
  page: number
  pageSize: number
  total: number
  onPageChange: (page: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  if (pages <= 1) return null
  return (
    <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
      <span className="tabular-nums">
        共 {formatNumber(total)} 条 · 第 {page + 1} / {pages} 页
      </span>
      <div className="flex gap-2">
        <Button variant="outline" size="sm" disabled={page <= 0} onClick={() => onPageChange(page - 1)}>
          上一页
        </Button>
        <Button variant="outline" size="sm" disabled={page + 1 >= pages} onClick={() => onPageChange(page + 1)}>
          下一页
        </Button>
      </div>
    </div>
  )
}
