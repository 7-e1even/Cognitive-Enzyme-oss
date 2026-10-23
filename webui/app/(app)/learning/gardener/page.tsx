import type { Metadata } from "next"

import { GardenerView } from "@/features/gardener/components/gardener-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("夜间园丁"),
  description: "在容器里跑 headless Claude Code,对照真实代码库复核记忆,带证据回写反馈或提交新假设;实时日志、环境自检与夜间定时。",
}

export default function GardenerPage() {
  return (
    <>
      <h1 className="sr-only">夜间园丁</h1>
      <GardenerView />
    </>
  )
}
