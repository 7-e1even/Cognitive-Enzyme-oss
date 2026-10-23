import { CopyButton } from "@/components/copy-button"

/** 可复制的命令 / 配置片段。 */
export function CodeSnippet({ title, code, language }: { title: string; code: string; language?: string }) {
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border">
      <div className="flex items-center justify-between gap-2 border-b bg-muted/40 px-3 py-1.5">
        <span className="truncate text-xs font-medium">{title}</span>
        <span className="flex items-center gap-2">
          {language ? <span className="font-mono text-xs text-muted-foreground">{language}</span> : null}
          <CopyButton value={code} label={title} />
        </span>
      </div>
      <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed">{code}</pre>
    </div>
  )
}
