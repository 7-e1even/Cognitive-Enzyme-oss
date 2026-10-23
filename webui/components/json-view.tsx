import { cn } from "@/lib/utils"

export function JsonView({ value, className }: { value: unknown; className?: string }) {
  return (
    <pre className={cn("max-h-72 overflow-auto rounded-lg border bg-muted/40 p-3 font-mono text-xs break-all whitespace-pre-wrap", className)}>
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}
