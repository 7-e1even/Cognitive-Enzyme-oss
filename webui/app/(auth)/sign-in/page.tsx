import type { Metadata } from "next"
import { Suspense } from "react"

import { SignInForm } from "@/features/auth/sign-in-form"

export const metadata: Metadata = { title: "登录" }

export default function SignInPage() {
  return (
    <main className="flex min-h-svh items-center justify-center bg-muted/30 p-6">
      <Suspense>
        <SignInForm />
      </Suspense>
    </main>
  )
}
