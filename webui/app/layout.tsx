import type { Metadata } from "next"
import { GeistMono } from "geist/font/mono"
import { GeistSans } from "geist/font/sans"

import { Providers } from "@/components/providers"
import { cn } from "@/lib/utils"

import "./globals.css"

export const metadata: Metadata = {
  title: { default: "Cognitive Enzyme", template: "%s · Cognitive Enzyme" },
  description: "Admin console for Cognitive Enzyme: memories, recall, learning and decision records.",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning className={cn("antialiased", GeistSans.variable, GeistMono.variable)}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  )
}
