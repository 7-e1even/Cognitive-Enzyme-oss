import type { Metadata } from "next"

import { ReviewView } from "@/features/review/components/review-view"
import { pageTitle } from "@/lib/routes"

export const metadata: Metadata = {
  title: pageTitle("隔离审核"),
  description: "质量门与免疫巡检拦下的可疑记忆,人工确认后才参与召回。",
}

export default function ReviewPage() {
  return (
    <>
      <h1 className="sr-only">隔离审核</h1>
      <ReviewView />
    </>
  )
}
