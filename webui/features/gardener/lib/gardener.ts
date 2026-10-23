import type { GardenerConfig, GardenerLastRun, GardenerStatus } from "@/lib/api/types"

/** 后端回显密钥用的掩码;原样发回去表示"不改" */
export const SECRET_MASK = "••••••••"

export const MODE_LABEL: Record<GardenerConfig["mode"], { label: string; short: string; description: string }> = {
  local: {
    label: "宿主机 claude CLI",
    short: "宿主机 CLI",
    description: "直接在后端所在机器上调用 claude 命令,以目标仓库为工作目录。需要宿主机装好 Claude Code。",
  },
  docker: {
    label: "docker 一次性容器",
    short: "docker 容器",
    description: "通过 Docker Engine API 拉起一次性 ce-gardener 容器,仓库只读挂载,跑完即删。本机不需要 docker CLI。",
  },
}

export const TRIGGER_LABEL: Record<string, string> = { manual: "手动运行", nightly: "夜间定时" }

export type RunOutcome = "running" | "succeeded" | "failed" | "stopped" | "timeout" | "idle"

export const OUTCOME_LABEL: Record<RunOutcome, { label: string; tone: "info-light" | "success-light" | "destructive-light" | "warning-light" | "outline" }> = {
  running: { label: "运行中", tone: "info-light" },
  succeeded: { label: "完成", tone: "success-light" },
  failed: { label: "失败", tone: "destructive-light" },
  stopped: { label: "已手动停止", tone: "warning-light" },
  timeout: { label: "超时终止", tone: "destructive-light" },
  idle: { label: "还没有运行过", tone: "outline" },
}

/** 一次运行的结局:手动停止与真实失败要分开说,超时单独说 */
export function outcomeOf(run: Pick<GardenerLastRun, "exit_code" | "error"> & { stopped?: boolean; running?: boolean; started_at?: string | null }): RunOutcome {
  if (run.running) return "running"
  if (!run.started_at) return "idle"
  if (run.stopped) return "stopped"
  if (run.error?.startsWith("超时")) return "timeout"
  if (run.error || (run.exit_code !== null && run.exit_code !== 0)) return "failed"
  return run.exit_code === 0 ? "succeeded" : "failed"
}

export function statusOutcome(status: GardenerStatus): RunOutcome {
  return outcomeOf({ ...status, stopped: false })
}

/** 秒数 → "3 分 12 秒" 一类;结束时间缺省时按现在算(运行中) */
export function durationSeconds(start: string | null, end: string | null, now: number) {
  if (!start) return null
  const from = new Date(start).getTime()
  const to = end ? new Date(end).getTime() : now
  if (Number.isNaN(from) || Number.isNaN(to)) return null
  return Math.max(0, Math.round((to - from) / 1000))
}

/** 日志行的级别:园丁自己的提示、错误、警告,其余是 Claude Code 的输出 */
export function lineLevel(line: string): "error" | "warning" | "info" | undefined {
  if (line.startsWith("[gardener]")) {
    if (/失败|错误/.test(line)) return "error"
    if (/超时|终止/.test(line)) return "warning"
    return "info"
  }
  // Claude Code 自己的输出只认明确的报错开头,正文里提到 error 的句子不算
  if (/^\s*(error|fatal)\b|^Traceback|\b\w*(Error|Exception):/i.test(line)) return "error"
  if (/^\s*warn(ing)?\b/i.test(line)) return "warning"
  return undefined
}

export const HOURS = Array.from({ length: 24 }, (_, hour) => ({
  value: String(hour),
  label: `${String(hour).padStart(2, "0")}:00`,
}))
