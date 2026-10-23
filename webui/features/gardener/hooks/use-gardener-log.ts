"use client"

import { useCallback, useEffect, useRef, useState } from "react"

import { gardenerApi } from "@/lib/api/endpoints"
import type { GardenerLog } from "@/lib/api/types"

import { lineLevel } from "../lib/gardener"

/** 与后端内存里的日志上限一致(deque maxlen=2000) */
const MAX_LINES = 2000
const POLL_MS = 1500

export interface LogLine {
  /** 服务器的绝对行号 */
  number: number
  text: string
  level?: "error" | "warning" | "info"
}

/** 纯文本行 → 带行号和级别的日志行,按园丁的日志约定标出报错 / 警告行。 */
export function textToLines(texts: string[], first = 1): LogLine[] {
  return texts.map((text, index) => ({ number: first + index, text, level: lineLevel(text) }))
}

/** 服务器返回的一段日志 → 日志行。行号是服务器的绝对行号(这一段的末行就是第 log_lines 行)。 */
function toLines(log: GardenerLog): LogLine[] {
  return textToLines(log.lines, log.log_lines - log.lines.length + 1)
}

/**
 * 园丁的实时日志。
 *
 * 续拉用服务器给的绝对计数(log_lines):日志队列满 2000 行会丢头,但计数单调递增,按它续拉不会错位。
 * 服务器开始了新一次运行(夜间定时恰好这时启动、或者别的标签页点了运行),计数从 0 重来、
 * started_at 也会变——这时整份重拉,而不是接在旧日志后面。
 *
 * 运行中每 1.5 秒拉一次,同一时刻只有一个请求在路上。
 * 看到运行从「进行中」变成「结束」时回调一次 onFinished。
 */
export function useGardenerLog({
  running,
  onUpdate,
  onFinished,
}: {
  running: boolean
  onUpdate?: (log: GardenerLog) => void
  onFinished?: (log: GardenerLog) => void
}) {
  const [lines, setLines] = useState<LogLine[]>([])
  const [loaded, setLoaded] = useState(false)
  const offset = useRef(0)
  const startedAt = useRef<string | null | undefined>(undefined)
  const busy = useRef(false)
  const wasRunning = useRef(false)
  const callbacks = useRef({ onUpdate, onFinished })

  useEffect(() => {
    callbacks.current = { onUpdate, onFinished }
  })

  const pull = useCallback(async () => {
    if (busy.current) return
    busy.current = true
    try {
      let log = await gardenerApi.log(offset.current)
      const newRun = log.started_at !== startedAt.current || log.log_lines < offset.current
      if (newRun && offset.current !== 0) log = await gardenerApi.log(0)
      const fresh = toLines(log)
      if (newRun) {
        startedAt.current = log.started_at
        setLines(fresh.slice(-MAX_LINES))
      } else if (fresh.length) {
        setLines((previous) => [...previous, ...fresh].slice(-MAX_LINES))
      }
      offset.current = log.log_lines
      setLoaded(true)
      callbacks.current.onUpdate?.(log)
      if (log.running) {
        wasRunning.current = true
      } else if (wasRunning.current) {
        wasRunning.current = false
        callbacks.current.onFinished?.(log)
      }
    } catch {
      // 轮询失败就等下一拍;连不上后端时页面其他地方会给出错误态
    } finally {
      busy.current = false
    }
  }, [])

  // 进页面先把服务器内存里现有的日志拿全(可能是正在跑的,也可能是上一次的)
  useEffect(() => {
    void pull()
  }, [pull])

  useEffect(() => {
    if (!running) return
    wasRunning.current = true
    const timer = setInterval(() => void pull(), POLL_MS)
    return () => clearInterval(timer)
  }, [running, pull])

  /** 刚点了运行:清掉旧日志,下一拍从头拉新一次运行的输出 */
  const restart = useCallback(() => {
    offset.current = 0
    startedAt.current = undefined
    wasRunning.current = true
    setLines([])
  }, [])

  return { lines, loaded, restart }
}
