"use client"

import { toast } from "sonner"
import { AlertCircleIcon, CircleCheckIcon } from "lucide-react"

/**
 * Save something as a file, and say that it happened.
 *
 * Every "export" on this console raised a toast claiming a file had been
 * prepared and produced nothing — which is worse than a dead button, because a
 * dead button at least does not lie about what it did. This writes the file.
 *
 * Both branches speak, the way the clipboard helper's do: a browser that will
 * not hand the page an object URL is a download that silently did not happen,
 * and the caller fires this and forgets it, so it never rejects.
 */
export function downloadJson(
  filename: string,
  value: unknown,
  label: string
): boolean {
  if (
    typeof document === "undefined" ||
    typeof URL === "undefined" ||
    typeof URL.createObjectURL !== "function"
  ) {
    toast.error(`${label} could not be exported`, {
      description: "This browser will not let the page write a file.",
      icon: TOAST_ERROR_ICON,
    })

    return false
  }

  const json = `${JSON.stringify(value, null, 2)}\n`
  const url = URL.createObjectURL(
    new Blob([json], { type: "application/json" })
  )
  const link = document.createElement("a")

  link.href = url
  link.download = filename
  // Appended before the click: a link outside the document does not download
  // in Firefox.
  document.body.appendChild(link)
  link.click()
  link.remove()

  // Not revoked inline. The click starts the download asynchronously, and a URL
  // released in the same tick can be gone before the browser has read it.
  setTimeout(() => URL.revokeObjectURL(url), 0)

  toast.success(`${label} exported`, {
    // The filename, not just the fact: it is what to look for in the folder the
    // browser dropped it into.
    description: filename,
    icon: TOAST_SUCCESS_ICON,
  })

  return true
}

const TOAST_SUCCESS_ICON = (
  <CircleCheckIcon className="size-[18px] text-green-600" aria-hidden="true" />
)

const TOAST_ERROR_ICON = (
  <AlertCircleIcon className="text-destructive size-[18px]" aria-hidden="true" />
)
