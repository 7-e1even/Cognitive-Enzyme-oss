import { Skeleton } from "@/components/ui/skeleton"

/** 静态预渲染阶段(useSearchParams 还没就绪)的占位。 */
export function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-32" />
        ))}
      </div>
      <Skeleton className="h-80" />
    </div>
  )
}
