"use client"

import { useCallback, useRef, useState, type ReactNode } from "react"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"

export interface ConfirmOptions {
  title: string
  description?: ReactNode
  confirmText?: string
  cancelText?: string
  destructive?: boolean
}

/**
 * 确认框:`const { confirm, confirmDialog } = useConfirm()`,
 * 在页面里渲染 `{confirmDialog}`,然后 `if (await confirm({...})) ...`。
 */
export function useConfirm() {
  const [state, setState] = useState<{ options: ConfirmOptions; open: boolean } | null>(null)
  const resolver = useRef<((ok: boolean) => void) | null>(null)

  const confirm = useCallback(
    (options: ConfirmOptions) =>
      new Promise<boolean>((resolve) => {
        resolver.current?.(false)
        resolver.current = resolve
        setState({ options, open: true })
      }),
    [],
  )

  const settle = (ok: boolean) => {
    resolver.current?.(ok)
    resolver.current = null
    setState((current) => (current ? { ...current, open: false } : current))
  }

  const confirmDialog = state ? (
    <AlertDialog
      open={state.open}
      onOpenChange={(open) => {
        if (!open) settle(false)
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{state.options.title}</AlertDialogTitle>
          {state.options.description ? (
            <AlertDialogDescription>{state.options.description}</AlertDialogDescription>
          ) : null}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{state.options.cancelText ?? "取消"}</AlertDialogCancel>
          <AlertDialogAction
            variant={state.options.destructive ? "destructive" : "default"}
            onClick={() => settle(true)}
          >
            {state.options.confirmText ?? "确定"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  ) : null

  return { confirm, confirmDialog }
}
