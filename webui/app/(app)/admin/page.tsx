import { redirect } from "next/navigation"

/** `/admin` 是分组,重定向到第一个子页。 */
export default function AdminPage() {
  redirect("/admin/users")
}
