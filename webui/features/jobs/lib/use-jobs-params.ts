"use client"

import { useCallback } from "react"

import { useUrlState } from "@/hooks/use-url-state"

import { parseStatus, type StatusTab } from "./data"

export const PAGE_SIZE = 20

export interface JobsParams {
  kind: string
  status: StatusTab
  /** 从 1 开始 */
  page: number
}

/** 列表的筛选、页签与分页放在 URL 里:刷新、分享、前进后退都能回到同一视图。 */
export function useJobsParams() {
  const [searchParams, update] = useUrlState()
  const params: JobsParams = {
    kind: searchParams.get("kind") ?? "",
    status: parseStatus(searchParams.get("status")),
    page: Math.max(1, Math.floor(Number(searchParams.get("page")) || 1)),
  }
  const setParams = useCallback(
    (patch: Partial<JobsParams>) => {
      // 换筛选条件时回到第一页,除非同时指定了页码
      const filterChanged = patch.kind !== undefined || patch.status !== undefined
      update({
        kind: patch.kind === undefined ? undefined : patch.kind || null,
        status: patch.status === undefined ? undefined : patch.status === "all" ? null : patch.status,
        page: patch.page !== undefined ? (patch.page > 1 ? patch.page : null) : filterChanged ? null : undefined,
      })
    },
    [update],
  )
  return { params, setParams }
}
