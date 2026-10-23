"use client"

import { Moon, Sun } from "lucide-react"
import { useTheme } from "next-themes"
import { usePathname } from "next/navigation"
import type { ReactNode } from "react"

import {
  Breadcrumb, BreadcrumbItem, BreadcrumbList, BreadcrumbPage, BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarInset, SidebarProvider, SidebarTrigger } from "@/components/ui/sidebar"
import { pageContainer, routeTrail } from "@/lib/routes"

import { AppSidebar } from "./app-sidebar"
import { AuthGuard } from "./auth-guard"

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname()
  const { resolvedTheme, setTheme } = useTheme()
  const trail = routeTrail(pathname)

  return (
    <AuthGuard>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
            <SidebarTrigger className="-ml-1" />
            <Separator orientation="vertical" className="mr-2 h-4" />
            <Breadcrumb>
              <BreadcrumbList>
                {trail.map((part, i) => (
                  <span key={part} className="contents">
                    {i > 0 ? <BreadcrumbSeparator /> : null}
                    <BreadcrumbItem>
                      <BreadcrumbPage className={i < trail.length - 1 ? "text-muted-foreground" : undefined}>{part}</BreadcrumbPage>
                    </BreadcrumbItem>
                  </span>
                ))}
              </BreadcrumbList>
            </Breadcrumb>
            <Button
              variant="ghost"
              size="icon"
              className="ml-auto"
              aria-label="切换外观"
              onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
            >
              <Sun className="dark:hidden" />
              <Moon className="hidden dark:block" />
            </Button>
          </header>
          <main className={`flex flex-1 flex-col gap-6 p-6 ${pageContainer(pathname)}`}>{children}</main>
        </SidebarInset>
      </SidebarProvider>
    </AuthGuard>
  )
}
