"use client"

import { TerminalSquareIcon } from "lucide-react"
import { useEffect, useMemo, useRef } from "react"

import { CopyButton } from "@/components/copy-button"
import { Spinner } from "@/components/spinner"
import { EmptyState } from "@/components/states"
import { ToneBadge } from "@/components/tone-badge"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { formatNumber } from "@/lib/format"
import { cn } from "@/lib/utils"

import type { LogLine } from "../hooks/use-gardener-log"

/** 把日志存成 .log 文件;浏览器不让页面写文件时静默放弃,按钮本身是附加功能。 */
function saveLog(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

const LEVEL_CLASS: Record<NonNullable<LogLine["level"]>, string> = {
  error: "bg-destructive/10 text-destructive",
  warning: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  info: "text-sky-700 dark:text-sky-400",
}

/**
 * 实时日志:等宽、带行号、贴底自动滚动(往上翻就停止跟随,翻回底部再接上),
 * 园丁自己的提示行和报错行各有底色。
 */
export function LogViewer({ lines, running, loaded, source }: { lines: LogLine[]; running: boolean; loaded: boolean; source: "live" | "tail" }) {
  const text = useMemo(() => lines.map((line) => line.text).join("\n"), [lines])
  const errors = useMemo(() => lines.filter((line) => line.level === "error").length, [lines])
  const scroller = useRef<HTMLDivElement | null>(null)
  const follow = useRef(true)

  useEffect(() => {
    const node = scroller.current
    if (node && follow.current) node.scrollTop = node.scrollHeight
  }, [lines])

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="flex-row items-center justify-between gap-3 border-b py-4">
        <div className="flex min-w-0 flex-col gap-1">
          <CardTitle className="flex items-center gap-2">
            运行日志
            {running ? (
              <ToneBadge tone="info-light">
                <Spinner className="size-3" /> 实时
              </ToneBadge>
            ) : null}
          </CardTitle>
          <CardDescription className="truncate text-xs">
            {source === "tail" ? "服务重启过,内存里的日志已清空;这是上次夜巡保存下来的最后 200 行" : running ? "每 1.5 秒追加一次;往上翻会暂停跟随,翻回底部继续" : "最近一次运行的完整输出"}
          </CardDescription>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {lines.length ? (
            <span className="text-xs text-muted-foreground tabular-nums">
              {formatNumber(lines.length)} 行{errors ? ` · ${errors} 行报错` : ""}
            </span>
          ) : null}
          {lines.length ? (
            <>
              <CopyButton value={text} label="日志" />
              <Button type="button" size="xs" variant="ghost" onClick={() => saveLog(`gardener-${Date.now()}.log`, text)}>
                下载
              </Button>
            </>
          ) : null}
        </div>
      </CardHeader>
      {!loaded ? (
        <div className="flex flex-col gap-2 p-4">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-4" style={{ width: `${40 + ((index * 37) % 55)}%` }} />
          ))}
        </div>
      ) : lines.length ? (
        <div
          ref={scroller}
          role="log"
          aria-label="园丁运行日志"
          className="h-[30rem] overflow-auto bg-muted/20 py-2 font-mono text-xs leading-5"
          onScroll={(event) => {
            const node = event.currentTarget
            follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 24
          }}
        >
          {lines.map((line) => (
            <div key={line.number} className={cn("flex gap-3 px-3", line.level && LEVEL_CLASS[line.level])}>
              <span className="w-10 shrink-0 text-right text-muted-foreground/70 select-none tabular-nums">{line.number}</span>
              <span className="min-w-0 break-all whitespace-pre-wrap">{line.text}</span>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState icon={TerminalSquareIcon} title="还没有日志" description="点「立即运行」开始一次夜巡,Claude Code 的输出会实时出现在这里。" />
      )}
    </Card>
  )
}
