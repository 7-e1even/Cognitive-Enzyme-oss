"use client"

import { ArrowRightIcon } from "lucide-react"
import Link from "next/link"
import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"

export interface NavSection {
  key: string
  label: string
  count: number
  dirty: number
  errors: number
}

/**
 * 分组目录:点一下滚到那一组,滚动时高亮当前读到的那一组。
 * 有未保存改动的组带一个主色圆点,有错误的带红点——长页面里一眼知道还剩哪里没处理。
 */
export function SectionNav({ sections }: { sections: NavSection[] }) {
  const [active, setActive] = useState<string | null>(null)
  const signature = sections.map((s) => s.key).join("|")

  useEffect(() => {
    const keys = signature ? signature.split("|") : []
    const visible = new Set<string>()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const key = entry.target.getAttribute("data-group")
          if (!key) continue
          if (entry.isIntersecting) visible.add(key)
          else visible.delete(key)
        }
        const first = keys.find((key) => visible.has(key))
        if (first) setActive(first)
      },
      { rootMargin: "-72px 0px -55% 0px", threshold: 0 },
    )
    for (const key of keys) {
      const element = document.getElementById(`group-${key}`)
      if (element) observer.observe(element)
    }
    return () => observer.disconnect()
  }, [signature])

  const current = active && sections.some((s) => s.key === active) ? active : sections[0]?.key

  return (
    <nav aria-label="参数分组" className="flex flex-col gap-0.5">
      <p className="px-3 pb-1.5 text-xs font-medium text-muted-foreground">分组</p>
      {sections.map((section) => (
        <button
          key={section.key}
          type="button"
          aria-current={current === section.key ? "true" : undefined}
          onClick={() => {
            setActive(section.key)
            document.getElementById(`group-${section.key}`)?.scrollIntoView({ behavior: "smooth", block: "start" })
          }}
          className={cn(
            "flex items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-start text-sm transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
            current === section.key ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
          )}
        >
          <span className="truncate">{section.label}</span>
          <span className="flex shrink-0 items-center gap-1.5">
            {section.errors > 0 ? (
              <span className="size-1.5 rounded-full bg-destructive" aria-label={`${section.errors} 项错误`} />
            ) : section.dirty > 0 ? (
              <span className="size-1.5 rounded-full bg-primary" aria-label={`${section.dirty} 项未保存`} />
            ) : null}
            <span className="text-xs font-normal text-muted-foreground tabular-nums">{section.count}</span>
          </span>
        </button>
      ))}
      <Link
        href="/models/bindings"
        className="mt-3 flex items-center justify-between gap-2 rounded-lg border border-dashed px-3 py-2 text-xs text-muted-foreground transition-colors hover:bg-muted/40 hover:text-foreground"
      >
        <span>模型绑定在单独一页</span>
        <ArrowRightIcon className="size-3.5 shrink-0" aria-hidden="true" />
      </Link>
    </nav>
  )
}
