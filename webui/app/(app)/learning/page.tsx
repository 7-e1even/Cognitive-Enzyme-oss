import { redirect } from "next/navigation"

/** `/learning` 是分组,不是页面:重定向到第一个子页,侧栏里只点亮一项。 */
export default function LearningPage() {
  redirect("/learning/consolidation")
}
