import { redirect } from "next/navigation"

/** `/models` 是分组,重定向到第一个子页。 */
export default function ModelsPage() {
  redirect("/models/providers")
}
