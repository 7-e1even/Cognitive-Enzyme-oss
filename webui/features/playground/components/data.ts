import type { PlaygroundCandidate, PlaygroundResult } from "@/lib/api/types"

// ── 表单 ────────────────────────────────────────────────────

export interface PrimeParams {
  context: string
  domain: string
  project: string
  max_results: number
  hook_min_conf: number
}

/** 与后端 PlaygroundPrime 的默认值一致(app/routers/admin.py)。 */
export const DEFAULT_PARAMS: PrimeParams = {
  context: "",
  domain: "",
  project: "",
  max_results: 15,
  hook_min_conf: 0.25,
}

// ── 运行记录(本标签页内保留,方便对比) ─────────────────────

export interface PrimeRun {
  id: number
  at: number
  params: PrimeParams
  result: PlaygroundResult
}

export const MAX_RUNS = 6
const RUNS_KEY = "ce:playground:runs"

/** 读本标签页里的运行记录;存储不可用(隐私模式等)或内容损坏时当作没有。 */
export function loadRuns(): PrimeRun[] {
  if (typeof window === "undefined") return []
  try {
    const raw = window.sessionStorage.getItem(RUNS_KEY)
    const runs = raw ? (JSON.parse(raw) as PrimeRun[]) : []
    return Array.isArray(runs) ? runs.filter((run) => run && run.result && run.params).slice(0, MAX_RUNS) : []
  } catch {
    return []
  }
}

export function saveRuns(runs: PrimeRun[]) {
  try {
    window.sessionStorage.setItem(RUNS_KEY, JSON.stringify(runs.slice(0, MAX_RUNS)))
  } catch {
    /* 存不下就只留在内存里 */
  }
}

// ── 判官与阶段 ──────────────────────────────────────────────

export const JUDGE_STATUS: Record<string, { label: string; tone: "success-light" | "warning-light" | "destructive-light" | "secondary" }> = {
  ok: { label: "正常作答", tone: "success-light" },
  timeout: { label: "超时", tone: "warning-light" },
  failed: { label: "调用失败", tone: "destructive-light" },
  off: { label: "未启用", tone: "secondary" },
}

export const LLM_JUDGE_STATUS: Record<string, string> = {
  ok: "正常作答",
  timeout: "超时(只用 Jev 的概率)",
  failed: "调用失败(只用 Jev 的概率)",
}

export const TIER: Record<string, { label: string; tone: "destructive-light" | "primary-light" | "secondary" }> = {
  critical: { label: "危急", tone: "destructive-light" },
  relevant: { label: "相关", tone: "primary-light" },
  peripheral: { label: "边缘", tone: "secondary" },
}

export type StageState = "done" | "warn" | "failed" | "skipped"

export interface Stage {
  key: string
  title: string
  /** 这一阶段之后还剩多少条;null 表示阶段没有执行 */
  count: number | null
  countLabel: string
  state: StageState
  detail: string
}

/**
 * 把后端的阶段计数整理成一条可读的流水线(与 app/matching/pipeline.py 的阶段一一对应)。
 * 数字都来自这一次运行的 trace,不在前端推算;说明文字只解释"这个数字意味着什么"。
 */
export function buildStages(result: PlaygroundResult): Stage[] {
  const { stages, status, candidates } = result
  const hookFiltered = candidates.filter((c) => c.injected && c.hook_filtered).length
  const exploratory = candidates.filter((c) => c.injected && c.exploratory).length
  const judgedStatus = status.judge || "off"

  const rerank: Stage = status.rerank_failed
    ? {
        key: "rerank",
        title: "精排",
        count: null,
        countLabel: "失败",
        state: "failed",
        detail: "精排调用失败(未配置、限流或超时),回退到模式打分,相关性下限这一关失效。",
      }
    : stages.reranked === null
      ? {
          key: "rerank",
          title: "精排",
          count: null,
          countLabel: "跳过",
          state: "skipped",
          detail: "没有可精排的候选,或没有配置精排模型。",
        }
      : {
          key: "rerank",
          title: "精排",
          count: stages.reranked,
          countLabel: `${stages.reranked} 条过线`,
          state: "done",
          detail: "精排模型逐条打相关分,低于相关性下限的直接淘汰——判官还可以把强烈认可的候选救回来。",
        }

  const judge: Stage =
    judgedStatus === "ok"
      ? {
          key: "judge",
          title: "判官",
          count: stages.judged,
          countLabel: `${stages.judged} 条有概率`,
          state: "done",
          detail: `${status.judge_model || "判官"} 为每条候选给出"对这个请求直接有用"的概率 p,并在一道单选题里给出最有用那条的概率 q。`,
        }
      : judgedStatus === "timeout"
        ? {
            key: "judge",
            title: "判官",
            count: null,
            countLabel: "超时",
            state: "warn",
            detail: `判官在时限内没有作答${status.judge_wait_ms ? `(等了 ${Math.round(status.judge_wait_ms)} 毫秒)` : ""},回退到按打分与固定剂量注入。`,
          }
        : judgedStatus === "failed"
          ? {
              key: "judge",
              title: "判官",
              count: null,
              countLabel: "失败",
              state: "failed",
              detail: "判官调用失败(未配置、网络或响应格式错误),回退到按打分与固定剂量注入。",
            }
          : {
              key: "judge",
              title: "判官",
              count: null,
              countLabel: "未启用",
              state: "skipped",
              detail: "没有绑定判官或判定已关闭,按打分与固定剂量注入。",
            }

  const injectedNotes = [
    exploratory ? `${exploratory} 条是冷启动探索项` : "",
    hookFiltered ? `${hookFiltered} 条会被 hook 的置信度过滤丢掉` : "",
  ].filter(Boolean)

  return [
    {
      key: "recall",
      title: "召回",
      count: stages.recalled,
      countLabel: `${stages.recalled} 条`,
      state: stages.recalled > 0 ? "done" : "warn",
      detail:
        stages.recalled > 0
          ? `向量(含查询扩写)、经验样本与词面(BM25)合并召回${stages.lexical ? `,其中 ${stages.lexical} 条有词面命中` : ""};都没命中时退回数据库关键词检索。只召回「启用」的记忆。`
          : "一条候选都没召回:检查领域筛选,或记忆库里有没有启用的相关记忆。",
    },
    {
      key: "conditions",
      title: "条件淘汰",
      count: Math.max(0, stages.recalled - stages.condition_killed),
      countLabel: stages.condition_killed ? `淘汰 ${stages.condition_killed} 条` : "无淘汰",
      state: "done",
      detail: "记忆自带的适用条件(conditions)不满足的候选在这里直接出局。",
    },
    rerank,
    {
      key: "score",
      title: "打分",
      count: stages.scored,
      countLabel: `${stages.scored} 条`,
      state: stages.scored > 0 ? "done" : "warn",
      detail: "综合置信度、模式匹配(或精排分)与关联加成打分,分成危急 / 相关 / 边缘三层。",
    },
    judge,
    {
      key: "inject",
      title: "注入",
      count: stages.injected,
      countLabel: `${stages.injected} 条`,
      state: "done",
      detail:
        stages.injected > 0
          ? `最终推送给智能体的记忆${injectedNotes.length ? `;${injectedNotes.join(",")}` : ""}。`
          : "这一次保持静默,一条都不推——不相关时不打扰,正是判官的职责。",
    },
  ]
}

// ── 候选 ────────────────────────────────────────────────────

/** 精排分两极分化(不相关的在 1e-4 量级),太小的数字直接写"<0.001"。 */
export function formatRerank(value: number | null) {
  if (value === null) return "—"
  if (value < 0.001) return "<0.001"
  return value.toFixed(3)
}

export function hasAnalogy(candidates: PlaygroundCandidate[]) {
  return candidates.some((c) => c.analog_p !== null || c.analog_q !== null)
}

export function excerpt(text: string, n = 48) {
  const flat = text.replace(/\s+/g, " ").trim()
  return flat.length > n ? `${flat.slice(0, n)}…` : flat
}
