import { redirect } from "next/navigation"

/** `/settings` 是分组,重定向到第一个子页。 */
export default function SettingsPage() {
  redirect("/settings/runtime")
}
