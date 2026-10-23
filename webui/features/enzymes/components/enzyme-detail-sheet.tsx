"use client"

import { CheckIcon, ChevronLeftIcon, ChevronRightIcon, CircleXIcon, SearchXIcon, Trash2Icon } from "lucide-react"
import type { KeyboardEvent } from "react"
import { toast } from "sonner"

import { useConfirm } from "@/components/confirm-dialog"
import { CopyButton } from "@/components/copy-button"
import { Spinner } from "@/components/spinner"
import { ErrorState } from "@/components/states"
import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Kbd } from "@/components/ui/kbd"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ApiError } from "@/lib/api/client"
import type { EnzymeDetail } from "@/lib/api/types"
import { shortId } from "@/lib/format"

import { useDeleteEnzyme, useEnzymeDetail, useEnzymeEvolution, useEnzymeFacets } from "../lib/queries"
import { EnzymeDetailBody } from "./enzyme-detail-body"
import { DomainLabel, OriginBadge, SeverityBadge, StatusBadge } from "./enzyme-badges"

const EVENTS_LIMIT = 20

export type ReviewDecision = "activate" | "reject" | "delete"

export interface ReviewControls {
  /** 在当前队列视图里的位置;打开的不是队列里的记忆时为 null */
  position: { index: number; total: number } | null
  onPrev: (() => void) | null
  onNext: (() => void) | null
  /** 已经确认过的决定;抽屉负责确认,调用方负责执行和切到下一条 */
  onDecide: (decision: ReviewDecision, enzyme: EnzymeDetail) => void
  pending: ReviewDecision | null
}

function SheetSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      {Array.from({ length: 8 }).map((_, index) => (
        <div key={index} className="grid grid-cols-[6.5rem_1fr] gap-4">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-full" />
        </div>
      ))}
    </div>
  )
}

/** 记忆和隔离审核共用这一个抽屉:内容相同,审核模式多一组「通过 / 拒绝」和上一条 / 下一条。 */
export function EnzymeDetailSheet({
  enzymeId,
  open,
  onOpenChange,
  onOpenEnzyme,
  onDeleted,
  review,
}: {
  /** 抽屉展示的记忆;关闭后保留,退场动画期间内容不消失 */
  enzymeId: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 打开另一条记忆(关联记忆、父记忆) */
  onOpenEnzyme: (id: string) => void
  onDeleted?: (id: string) => void
  review?: ReviewControls
}) {
  const detailQuery = useEnzymeDetail(enzymeId)
  const events = useEnzymeEvolution(enzymeId, EVENTS_LIMIT)
  const facets = useEnzymeFacets()
  const remove = useDeleteEnzyme()
  const { confirm, confirmDialog } = useConfirm()
  const detail = detailQuery.data
  const notFound = detailQuery.error instanceof ApiError && detailQuery.error.status === 404
  const inQueue = detail?.status === "quarantine"

  async function askDelete(enzyme: EnzymeDetail) {
    const ok = await confirm({
      title: "删除这条记忆?",
      description: `「${enzyme.suggestion}」会从库里移除,之后不再参与召回,且无法恢复。只是不想让它被召回的话,把状态改成「已拒绝」即可。`,
      confirmText: "删除",
      destructive: true,
    })
    if (!ok) return
    if (review) {
      review.onDecide("delete", enzyme)
      return
    }
    try {
      await remove.mutateAsync(enzyme.id)
      toast.success("已删除记忆", { description: enzyme.suggestion })
      onOpenChange(false)
      onDeleted?.(enzyme.id)
    } catch {
      // 失败原因由接口错误提示给出
    }
  }

  async function askReject(enzyme: EnzymeDetail) {
    if (!review) return
    const ok = await confirm({
      title: "拒绝这条记忆?",
      description: "被拒绝的记忆保留在库里,但不再参与召回;之后可以在记忆页里把状态改回「启用」。",
      confirmText: "拒绝",
      destructive: true,
    })
    if (ok) review.onDecide("reject", enzyme)
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!review || !detail || review.pending) return
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || event.nativeEvent.isComposing) return
    const target = event.target as HTMLElement
    if (target.closest("input, textarea, select, [contenteditable='true'], [role='listbox'], [role='menu'], [role='combobox']")) return
    const key = event.key.toLowerCase()
    if (key === "j" && review.onNext) review.onNext()
    else if (key === "k" && review.onPrev) review.onPrev()
    else if (key === "a" && inQueue) review.onDecide("activate", detail)
    else if (key === "r" && inQueue) void askReject(detail)
    else return
    event.preventDefault()
  }

  return (
    <>
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent
          side="right"
          showCloseButton
          tabIndex={-1}
          onKeyDown={onKeyDown}
          // 正在编辑某一行时,Esc 只退出这一行,不关抽屉
          onEscapeKeyDown={(event) => {
            if (document.activeElement?.closest("[data-editing]")) event.preventDefault()
          }}
          className="gap-0 outline-none data-[side=right]:w-full data-[side=right]:sm:max-w-xl"
        >
          <SheetHeader className="shrink-0 gap-2.5 border-b pr-12">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <SheetTitle className="line-clamp-2 text-sm leading-snug">
                  {detail?.suggestion ?? (notFound ? "找不到这条记忆" : "记忆详情")}
                </SheetTitle>
                <SheetDescription className="mt-1 flex min-w-0 items-center gap-1.5 text-xs">
                  {detail ? (
                    <>
                      <DomainLabel domain={detail.domain} className="max-w-40" />
                      <span aria-hidden="true">·</span>
                    </>
                  ) : null}
                  <span className="font-mono" title={enzymeId ?? undefined}>
                    {shortId(enzymeId)}
                  </span>
                  {enzymeId ? <CopyButton value={enzymeId} label=" ID" size="icon-xs" /> : null}
                </SheetDescription>
              </div>
              {review?.position ? (
                <div className="flex shrink-0 items-center gap-0.5">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button type="button" variant="ghost" size="icon-sm" aria-label="上一条" disabled={!review.onPrev} onClick={() => review.onPrev?.()}>
                        <ChevronLeftIcon />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      上一条 <Kbd>K</Kbd>
                    </TooltipContent>
                  </Tooltip>
                  <span className="min-w-12 text-center text-xs text-muted-foreground tabular-nums">
                    {review.position.index + 1} / {review.position.total}
                  </span>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button type="button" variant="ghost" size="icon-sm" aria-label="下一条" disabled={!review.onNext} onClick={() => review.onNext?.()}>
                        <ChevronRightIcon />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>
                      下一条 <Kbd>J</Kbd>
                    </TooltipContent>
                  </Tooltip>
                </div>
              ) : null}
            </div>
            {detail ? (
              <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status={detail.status} />
                <SeverityBadge severity={detail.severity} />
                <OriginBadge origin={detail.origin} />
                <span className="text-xs text-muted-foreground">第 {detail.version} 版</span>
              </div>
            ) : null}
          </SheetHeader>

          <div className="min-h-0 flex-1">
            {detail ? (
              <ScrollArea key={detail.id} className="h-full">
                <EnzymeDetailBody
                  detail={detail}
                  events={events.data}
                  eventsLoading={events.isLoading}
                  eventsLimit={EVENTS_LIMIT}
                  domains={facets.data?.domain ?? {}}
                  onOpenEnzyme={onOpenEnzyme}
                />
              </ScrollArea>
            ) : notFound ? (
              <Empty className="h-full border-0">
                <EmptyHeader>
                  <EmptyMedia variant="icon">
                    <SearchXIcon />
                  </EmptyMedia>
                  <EmptyTitle>找不到这条记忆</EmptyTitle>
                  <EmptyDescription>它可能已被删除,或在巩固时合并进了另一条记忆。</EmptyDescription>
                </EmptyHeader>
              </Empty>
            ) : detailQuery.isError ? (
              <div className="p-4">
                <ErrorState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
              </div>
            ) : (
              <SheetSkeleton />
            )}
          </div>

          {detail ? (
            <SheetFooter className="shrink-0 flex-row items-center justify-between gap-3 border-t">
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="shrink-0 text-destructive hover:text-destructive"
                disabled={remove.isPending || Boolean(review?.pending)}
                onClick={() => void askDelete(detail)}
              >
                {remove.isPending || review?.pending === "delete" ? <Spinner /> : <Trash2Icon />}
                删除
              </Button>
              {review && inQueue ? (
                <div className="flex items-center gap-2">
                  <Button type="button" size="sm" variant="outline" disabled={review.pending !== null} onClick={() => void askReject(detail)}>
                    {review.pending === "reject" ? <Spinner /> : <CircleXIcon />}
                    拒绝
                    <Kbd className="ml-1 hidden sm:inline-flex">R</Kbd>
                  </Button>
                  <Button type="button" size="sm" disabled={review.pending !== null} onClick={() => review.onDecide("activate", detail)}>
                    {review.pending === "activate" ? <Spinner /> : <CheckIcon />}
                    通过
                    <Kbd className="ml-1 hidden sm:inline-flex">A</Kbd>
                  </Button>
                </div>
              ) : null}
            </SheetFooter>
          ) : null}
        </SheetContent>
      </Sheet>
      {confirmDialog}
    </>
  )
}
