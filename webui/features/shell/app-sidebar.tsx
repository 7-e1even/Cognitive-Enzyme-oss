"use client"

import { Brain, LogOut, UserCog } from "lucide-react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { useState } from "react"

import {
  Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader,
  SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarRail,
} from "@/components/ui/sidebar"
import { useMe } from "@/lib/api/hooks"
import { logout } from "@/lib/auth"
import { NAV } from "@/lib/nav"

import { AccountDialog } from "./account-dialog"

export function AppSidebar() {
  const pathname = usePathname()
  const me = useMe().data
  const [accountOpen, setAccountOpen] = useState(false)
  const active = (href: string) => (href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`))

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild>
              <Link href="/">
                <div className="flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground">
                  <Brain className="size-4" />
                </div>
                <div className="grid text-left text-sm leading-tight">
                  <span className="truncate font-semibold">Cognitive Enzyme</span>
                  <span className="truncate text-xs text-muted-foreground">System 1 记忆层</span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        {NAV.map((group) => {
          const items = group.items.filter((i) => !i.adminOnly || me?.role === "admin")
          if (!items.length) return null
          return (
            <SidebarGroup key={group.label}>
              <SidebarGroupLabel>{group.label}</SidebarGroupLabel>
              <SidebarGroupContent>
                <SidebarMenu>
                  {items.map((item) => (
                    <SidebarMenuItem key={item.href}>
                      <SidebarMenuButton asChild isActive={active(item.href)} tooltip={item.title}>
                        <Link href={item.href}>
                          <item.icon />
                          <span>{item.title}</span>
                        </Link>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          )
        })}
      </SidebarContent>
      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={() => setAccountOpen(true)} tooltip="账户设置">
              <UserCog />
              <span className="truncate">账户设置</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
          <SidebarMenuItem>
            <SidebarMenuButton onClick={logout} tooltip="退出登录">
              <LogOut />
              <span className="truncate">{me ? `${me.display_name || me.username} · 退出` : "退出登录"}</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
      <SidebarRail />
      <AccountDialog open={accountOpen} onOpenChange={setAccountOpen} />
    </Sidebar>
  )
}
