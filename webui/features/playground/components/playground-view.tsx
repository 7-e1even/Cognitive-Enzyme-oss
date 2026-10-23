"use client"

import { useCallback } from "react"
import { usePathname, useSearchParams } from "next/navigation"

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

import { PrimePanel } from "./prime-panel"
import { SemanticSearch } from "./semantic-search"

type Tab = "prime" | "search"

/** 页签与检索词放在 URL 里,可以分享;表单草稿与运行记录留在本标签页。 */
function usePlaygroundParams() {
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const tab: Tab = searchParams.get("tab") === "search" ? "search" : "prime"
  const q = searchParams.get("q") ?? ""
  const domain = searchParams.get("domain") ?? ""

  const setParams = useCallback(
    (patch: Partial<{ tab: Tab; q: string; domain: string }>) => {
      const next = new URLSearchParams(searchParams.toString())
      for (const [key, value] of Object.entries(patch)) {
        if (value === undefined) continue
        if (!value || (key === "tab" && value === "prime")) next.delete(key)
        else next.set(key, value)
      }
      const qs = next.toString()
      window.history.replaceState(null, "", qs ? `${pathname}?${qs}` : pathname)
    },
    [pathname, searchParams],
  )

  return { tab, q, domain, setParams }
}

export function PlaygroundView() {
  const { tab, q, domain, setParams } = usePlaygroundParams()

  return (
    <Tabs value={tab} onValueChange={(value) => setParams({ tab: value as Tab })} className="gap-4">
      <TabsList variant="line">
        <TabsTrigger value="prime">模拟 prime</TabsTrigger>
        <TabsTrigger value="search">语义检索</TabsTrigger>
      </TabsList>
      <TabsContent value="prime">
        <PrimePanel />
      </TabsContent>
      <TabsContent value="search">
        <SemanticSearch q={q} domain={domain} onSearch={(nextQ, nextDomain) => setParams({ tab: "search", q: nextQ, domain: nextDomain })} />
      </TabsContent>
    </Tabs>
  )
}
