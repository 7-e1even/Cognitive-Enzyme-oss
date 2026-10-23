import type { ReactNode } from "react"
import type { Edge, Node } from "@xyflow/react"
import {
  BanIcon,
  BrainIcon,
  CalendarClockIcon,
  CircleOffIcon,
  DatabaseIcon,
  FolderIcon,
  GitMergeIcon,
  ListFilterIcon,
  MessageSquareIcon,
  MessagesSquareIcon,
  MoonStarIcon,
  RefreshCwIcon,
  RouteIcon,
  ScaleIcon,
  SearchIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  SparklesIcon,
  SyringeIcon,
  ThumbsUpIcon,
  TrendingDownIcon,
} from "lucide-react"

import type { Dashboard, FlowFacts, HomeSummary } from "@/lib/api/types"
import { formatNumber } from "@/lib/format"
import { relativeTime } from "@/lib/relative-time"

/**
 * 整个系统的三条线,画在一张 React Flow 画布上:
 *
 * - 召回(每次提问):提问 → 领域分流 → 召回 → 精排 → 适用性判官 →(适用)注入上下文 → 事后反馈 /(都不适用)不注入
 * - 学习(每次会话结束):会话结束 → 提炼经验 → 已会过滤 → 质量门 → 领域归类 →(通过)入库 /(可疑)隔离审核 →(确认)入库;
 *   模型闭卷就会的、质量分太低的不入库
 * - 巩固(定时):定时巩固 → 衰减与清理 → 合并去重 → 做梦与回放 → 规则与索引
 *
 * 卡片和侧栏里的数字、开关、阈值都来自实际配置(/api/flow)与实时统计,改了运行参数页面跟着变;
 * 读不到的显示「—」。和代码对不上就是这里错了——改流程时一起改这里。
 * 纯函数:组件拿接口数据调它。
 */

export type StepKind = "trigger" | "condition" | "action"
export type StepStatus = "ok" | "warn" | "off" | "idle"

export type StepNodeData = {
  kind: StepKind
  icon: ReactNode
  title: string
  detail: string
  /** 底部左边:这一步的实时数字或关键参数。 */
  metric: string
  status: StepStatus
  /** 不用默认文案时的状态标签,如「待审 3」「已关闭」。 */
  statusLabel?: string
  /** 这一步具体怎么做,按实际配置写;点卡片在侧栏里看。 */
  rules: string[]
  /** 侧栏底部「前往」去哪儿。 */
  href: string
  linkLabel: string
}

export type LaneNodeData = { title: string; cadence: string }

export type StepNodeType = Node<StepNodeData, "step">
export type LaneNodeType = Node<LaneNodeData, "lane">
export type FlowNode = StepNodeType | LaneNodeType

// 列宽 300(卡片 240 + 连线 60);三条线上下分开,每条线的名字标在第一张卡片上方。
// 分支上下错开:召回的「注入 / 不注入」各 ±70;学习线「隔离」「不入库」在下面一排
const COL = 300
const LANE = { recall: 0, learn: 320, consolidate: 660 }
const LANE_LABEL_OFFSET = 44

function step(id: string, col: number, y: number, data: StepNodeData): StepNodeType {
  return { id, type: "step", position: { x: col * COL, y }, data, draggable: false, selectable: false }
}

function lane(id: string, y: number, data: LaneNodeData): LaneNodeType {
  return { id, type: "lane", position: { x: 0, y: y - LANE_LABEL_OFFSET }, data, draggable: false, selectable: false }
}

function edge(source: string, target: string, label?: string, animated = false): Edge {
  return { id: `${source}-${target}`, source, target, label, type: "labeled", animated, selectable: false }
}

/** 运行参数的读法:读不到时数字是「—」、开关是 undefined(界面按「读取中」处理)。 */
function reader(facts: FlowFacts | undefined) {
  const s = facts?.settings
  const num = (key: string): string => {
    const value = s?.[key]
    if (typeof value !== "number") return "—"
    return Number.isInteger(value) ? formatNumber(value) : String(value)
  }
  const pct = (key: string): string => {
    const value = s?.[key]
    return typeof value === "number" ? `${Math.round(value * 100)}%` : "—"
  }
  const on = (key: string): boolean | undefined => (s ? Boolean(s[key]) && s[key] !== "off" : undefined)
  const raw = (key: string) => s?.[key]
  return { num, pct, on, raw }
}

function duration(seconds: number): string {
  if (seconds < 3600) return `${Math.max(1, Math.round(seconds / 60))} 分钟`
  const hours = seconds / 3600
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} 小时`
}

export function buildFlow(
  home: HomeSummary | undefined,
  dashboard: Dashboard | undefined,
  facts: FlowFacts | undefined,
  domainCount: number | undefined,
  isAdmin: boolean,
): { nodes: FlowNode[]; edges: Edge[] } {
  const caps = home?.capabilities
  const { num, pct, on, raw } = reader(facts)
  const n = (value: number | undefined) => (value === undefined ? "—" : formatNumber(value))
  const days = home?.impact.days ?? 7
  const captures = dashboard?.activity.reduce((sum, point) => sum + point.captures, 0)
  const quality = facts?.quality
  const schedule = facts?.schedule
  // 模型没配时去绑定页;操作员进不去,留在调试页
  const bindings = isAdmin ? "/models/bindings" : "/playground"
  const model = (bound: boolean | undefined, optional = false): Pick<StepNodeData, "status" | "statusLabel"> =>
    bound === undefined
      ? { status: "idle", statusLabel: "读取中" }
      : bound
        ? { status: "ok" }
        : optional
          ? { status: "off", statusLabel: "可选" }
          : { status: "off" }
  const toggle = (value: boolean | undefined, okLabel?: string): Pick<StepNodeData, "status" | "statusLabel"> =>
    value === undefined
      ? { status: "idle", statusLabel: "读取中" }
      : value
        ? { status: "ok", statusLabel: okLabel }
        : { status: "off", statusLabel: "已关闭" }
  const onOff = (value: boolean | undefined) => (value === undefined ? "—" : value ? "开" : "关")

  // 判官能不能用:关了或没配就按固定剂量注入
  const judgeOn = on("judge.enabled") === false ? false : caps?.judge
  const rescue = on("judge.rescue")
  // 类比识别三档:on 才会注入,shadow 只问不用
  const analogy = facts ? raw("exemplar.analogy") === "on" : undefined
  const dedup = on("learning.dedup")
  const skipKnown = on("learning.skip_known")
  const domainAuto = on("domain.auto")
  const routeMode = facts ? String(raw("recall.domain_route")) : undefined

  const nodes: FlowNode[] = [
    lane("lane-recall", LANE.recall - 70, { title: "召回", cadence: "每次提问" }),
    lane("lane-learn", LANE.learn, { title: "学习", cadence: "每次会话结束" }),
    lane("lane-consolidate", LANE.consolidate, { title: "巩固", cadence: "后台定时" }),

    // ── 召回 ──
    step("ask", 0, LANE.recall, {
      kind: "trigger",
      icon: <MessageSquareIcon />,
      title: "智能体提问",
      detail: "UserPromptSubmit hook 调用 prime",
      metric: `近 ${days} 天 ${n(home?.impact.primes)} 次`,
      status: home ? (home.last_prime_at ? "ok" : "idle") : "idle",
      statusLabel: home ? (home.last_prime_at ? "已接入" : "未接入") : "读取中",
      rules: [
        "每次在 Claude Code 里提问前,hook 把当前情境发给 prime,拿回要注入的记忆。",
        "hook 默认 9 秒没拿到结果就不注入,不耽误提问。",
      ],
      href: isAdmin ? "/settings/integrations" : "/playground",
      linkLabel: isAdmin ? "接入设置" : "调试台",
    }),
    step("route", 1, LANE.recall, {
      kind: "condition",
      icon: <RouteIcon />,
      title: "领域分流",
      detail:
        routeMode === undefined
          ? "读取中"
          : routeMode === "off"
            ? "已关闭:全库一起找"
            : routeMode === "strict"
              ? "严格:只在选中的领域里找"
              : "专家通道:全库照常,再到选中的领域补召回",
      metric: `门槛 ${num("recall.domain_route_q")} · 最多 ${num("recall.domain_route_k")} 个领域`,
      ...(routeMode === undefined
        ? { status: "idle" as const, statusLabel: "读取中" }
        : routeMode === "off"
          ? toggle(false)
          : caps?.judge === false
            ? { status: "off" as const, statusLabel: "未配置判官" }
            : { status: "ok" as const, statusLabel: routeMode === "strict" ? "严格" : "专家通道" }),
      rules: [
        "判官一道单选:处理这个提问要用到哪个领域的知识(选项是有启用记忆的领域,名称和一句话描述),都不需要或只是通用问题就选 none。",
        `单选概率 ≥ ${num("recall.domain_route_q")} 且高于 none 的领域按概率选中,最多 ${num("recall.domain_route_k")} 个。`,
        routeMode === "off"
          ? "当前关闭:全库一起找。"
          : routeMode === "strict"
            ? `严格:只留选中领域里的候选(也会到这些领域里各补召回 ${num("recall.domain_route_limit")} 条)。`
            : `专家通道:全库召回照常,再到每个选中的领域里补召回 ${num("recall.domain_route_limit")} 条,补进来的另留名额、不挤占原有召回,一起交给精排。`,
        `和向量检索并行发出;检索完后最多再等 ${num("recall.domain_route_timeout")} 秒,判官超时、出错或判为通用问题时照常全库召回。`,
        "hook 设了 CE_DOMAIN 时直接只在那个领域里找,不再分流。领域本身在入库时自动归类,见学习线。",
      ],
      href: caps?.judge === false ? bindings : isAdmin ? "/settings/runtime" : "/playground",
      linkLabel: caps?.judge === false ? "绑定判官" : isAdmin ? "运行参数" : "调试台",
    }),
    step("recall", 2, LANE.recall, {
      kind: "action",
      icon: <SearchIcon />,
      title: "召回",
      detail: ["向量", on("exemplar.recall") === false ? "" : "经验样本", on("recall.lexical") === false ? "" : "词面(BM25)"]
        .filter(Boolean)
        .join(" + "),
      metric: `${n(dashboard?.by_status.active)} 条启用的记忆`,
      ...model(caps?.embedding),
      rules: [
        "向量检索:按语义找最相近的约 50 条。",
        on("exemplar.recall") === false
          ? "经验样本召回:已关闭。"
          : "经验样本:当前情境也和每条记忆过去适用过的情境比对,见过类似情况就能认出来。",
        on("recall.lexical") === false
          ? "词面召回:已关闭。"
          : `词面召回(BM25):再并入前 ${num("recall.lexical_limit")} 条,补上只共享罕见词的情况。`,
        `查询扩写:${onOff(on("recall.query_expand"))}。`,
        routeMode === undefined
          ? "—"
          : routeMode === "off"
            ? "领域分流关闭:全库一起找;hook 设了 CE_DOMAIN 时只在那个领域里找。"
            : `领域分流选中领域时:${routeMode === "strict" ? "只留这些领域的候选" : "再到这些领域里各补召回一轮"}(见上一步)。`,
      ],
      href: caps?.embedding === false ? bindings : "/playground",
      linkLabel: caps?.embedding === false ? "绑定向量模型" : "调试台",
    }),
    step("rerank", 3, LANE.recall, {
      kind: "action",
      icon: <ListFilterIcon />,
      title: "精排",
      detail: "重排序模型打相关性分",
      metric: `相关性下限 ${num("ranking.relevance_floor")}`,
      ...model(caps?.rerank, true),
      rules: [
        "重排序模型给每条候选打相关性分,按分数排序;没配精排模型时退回按触发词匹配打分。",
        rescue === false
          ? `分数低于下限 ${num("ranking.relevance_floor")} 的直接淘汰(救援已关闭)。`
          : `分数低于下限 ${num("ranking.relevance_floor")} 的不进候选;判官强烈认可时可以救回(见下一步)。`,
      ],
      href: caps?.rerank === false ? bindings : "/playground",
      linkLabel: caps?.rerank === false ? "绑定精排模型" : "调试台",
    }),
    step("judge", 4, LANE.recall, {
      kind: "condition",
      icon: <ScaleIcon />,
      title: "适用性判官",
      detail: judgeOn === false ? "未启用:按固定剂量注入" : "逐条判断 + 单选:哪条最有用 / 都没用",
      metric: `判定池 ${num("judge.pool")} 条 · 超时 ${num("judge.timeout")} 秒`,
      ...(on("judge.enabled") === false ? toggle(false) : model(caps?.judge, true)),
      rules: [
        `每次提问只问判官一次,候选最多 ${num("judge.pool")} 条:精排后的在前${rescue === false ? "" : ",再按召回顺序补足(救援用)"}${analogy === false ? "" : `;另加最多 ${num("exemplar.analogy_k")} 条类比候选`}。`,
        "每条候选一道是非题「对这个请求是否直接有用」(概率 p),再加一道单选「哪条最有用,还是都没用」(概率 q 与 none)。",
        `主选:单选最高的那条,q ≥ ${num("selection.choice_min")} 且 p ≥ ${num("selection.primary_p")};或 q ≥ ${num("selection.choice_override")};或 none < ${num("selection.none_max")} 且 p ≥ ${num("selection.supplement_p")}。`,
        `补充:none < ${num("selection.none_max")} 时,已有主选的前提下其余 p ≥ ${num("selection.supplement_p")} 且单选概率不低于最高项一成的也注入;危急级的 p ≥ ${num("selection.critical_p")} 即可。`,
        rescue === false
          ? "救援:已关闭。"
          : `救回来的候选要更强的证据:p ≥ ${num("selection.rescue_p")} 或 q ≥ ${num("selection.rescue_q")}。`,
        analogy === false
          ? "类比识别:已关闭。"
          : `类比:当前情境和某条记忆过去适用过的情境是同一类(单选 ≥ ${num("selection.analogy_q")}、逐条 ≥ ${num("selection.analogy_p")}),也注入。`,
        `单选判「都没用」≥ ${num("selection.none_max")} 时一条都不注入。`,
        `新记忆置信度低于 ${num("judge.promote_below")} 但 p ≥ ${num("judge.promote_p")} 的,每次最多放行一条,标为探索。`,
        `探索槽:有 ${pct("ranking.exploration_rate")} 的概率再放一条演化出来的假设(p ≥ ${num("judge.explore_p")}),标为假设。`,
        `判官没配、超时或出错时退回固定剂量:危急级全留,其余按分数取前 ${num("ranking.dosage_budget")} 条。`,
        `LLM 第二判官:${onOff(on("judge.llm"))}${on("judge.llm") ? "(与判官并行,逐条概率取平均)" : ""}。`,
      ],
      href: caps?.judge === false ? bindings : isAdmin ? "/settings/runtime" : "/playground",
      linkLabel: caps?.judge === false ? "绑定判官" : isAdmin ? "选择阈值" : "调试台",
    }),
    step("inject", 5, LANE.recall - 70, {
      kind: "action",
      icon: <SyringeIcon />,
      title: "注入上下文",
      detail: "写进这次提问的上下文",
      metric: `近 ${days} 天注入 ${n(home?.impact.injected)} 条`,
      status: "ok",
      rules: [
        "hook 默认只注入记忆自身置信度 ≥ 0.25 的(探索项例外),最多 6 条。",
        "注入块里带着每条记忆的 id,智能体可以事后 report 用没用上。",
      ],
      href: "/learning/metrics",
      linkLabel: "详细指标",
    }),
    step("skip", 5, LANE.recall + 70, {
      kind: "action",
      icon: <CircleOffIcon />,
      title: "不注入",
      detail: "都不适用,或没按时拿到结果",
      metric: on("distill.decision_log") === false ? "判定记录已关闭" : "判定记下来,用于训练判官",
      status: "idle",
      statusLabel: "静默",
      rules: [
        "判官判「都没用」、没有候选过线,或 hook 9 秒内没拿到结果时,这次什么都不注入。",
        on("distill.decision_log") === false
          ? "判定记录:已关闭。"
          : "每次判定的完整输入和结果都记下来,作为训练自己判官的数据(判定记录页可看)。",
      ],
      href: isAdmin ? "/learning/distill" : "/playground",
      linkLabel: isAdmin ? "判定记录" : "调试台",
    }),
    step("feedback", 6, LANE.recall - 70, {
      kind: "action",
      icon: <ThumbsUpIcon />,
      title: "事后反馈",
      detail: "有用、没用还是说反了",
      metric: home ? `${n(home.impact.helped)} 有用 · ${n(home.impact.harmed)} 说反` : "—",
      status: "ok",
      rules: [
        "反馈有两个来源:智能体调用 report,或会话结束时按对话记录评估这次注入过的记忆。",
        on("judge.credit") === false
          ? "有用的加置信度,说反了的扣。"
          : "有用的加置信度,说反了的扣;判官当时认为越适用却失败的,扣得越重。",
        on("exemplar.learn") === false
          ? "经验样本学习:已关闭。"
          : "有用时,这次的情境记为这条记忆的经验样本,下次遇到类似情境更容易认出来。",
      ],
      href: "/learning/metrics",
      linkLabel: "详细指标",
    }),

    // ── 学习 ──
    step("session", 0, LANE.learn, {
      kind: "trigger",
      icon: <MessagesSquareIcon />,
      title: "会话结束",
      detail: "SessionEnd hook 提交对话记录",
      metric: captures === undefined ? "—" : `近期 ${formatNumber(captures)} 次`,
      status: "ok",
      rules: [
        "会话结束时 hook 把对话记录交给后台排队,自己立即返回。",
        "hook 默认最多发 400 轮、20 万字符;超出时保留开头两轮和最近的轮次。",
      ],
      href: isAdmin ? "/admin/jobs" : "/enzymes",
      linkLabel: isAdmin ? "后台任务" : "记忆",
    }),
    step("extract", 1, LANE.learn, {
      kind: "action",
      icon: <SparklesIcon />,
      title: "提炼经验",
      detail: "语言模型从会话里总结",
      metric: "新增 · 更新 · 强化 · 作废",
      ...model(caps?.llm),
      rules: [
        `读取对话记录,上限 ${num("learning.capture_chars")} 字符;超出时保留开头一段和大部分结尾。`,
        "一次最多提炼 8 条候选;每条先找相似的已有记忆:没有相似的直接当新经验,有相似的由语言模型判定新增、更新、强化、作废还是丢弃。",
        "只有新经验走质量门;更新、强化、作废直接作用在已有记忆上。",
        "同时评估这次会话里注入过的记忆(见事后反馈),并写一张情景卡。",
      ],
      href: caps?.llm === false ? bindings : isAdmin ? "/admin/jobs" : "/enzymes",
      linkLabel: caps?.llm === false ? "绑定语言模型" : isAdmin ? "后台任务" : "记忆",
    }),
    step("known", 2, LANE.learn, {
      kind: "condition",
      icon: <BrainIcon />,
      title: "已会过滤",
      detail: skipKnown === false ? "已关闭:候选都往下走" : "模型闭卷就会的经验不入库",
      metric: skipKnown === undefined ? "—" : skipKnown ? "每条候选多 2 次调用" : "可在运行参数里开启",
      ...toggle(skipKnown),
      rules: [
        "教科书式的通用方法(先算简单情形、分阶段推进……)预训练里早就有,存下来只会占注入名额、稀释真正有价值的经验。",
        "开启后,每条候选先让引擎语言模型只看触发情境、不给任何记忆,闭卷答一遍;再判断这份答案是否已经涵盖候选经验的核心要点,涵盖了就不入库。",
        "有闭卷回答没提到的具体条件、关键步骤、反直觉的结论或特定的坑,照常往下走。",
        "闭卷作答或评审失败(模型没绑、超时)时不拦,照常往下走。默认关闭。",
        "只拦新增:撞上已有记忆的候选照常走强化/补充;文档导入、harvest 接口、白日梦与交叉产生的新记忆也过这一步。",
      ],
      href: isAdmin ? "/settings/runtime" : "/enzymes",
      linkLabel: isAdmin ? "运行参数" : "记忆",
    }),
    step("known-skip", 3, LANE.learn + 130, {
      kind: "action",
      icon: <CircleOffIcon />,
      title: "不入库",
      detail: "模型已经会了",
      metric: "计入「跳过 · 模型已会」",
      status: "idle",
      statusLabel: "丢弃",
      rules: ["闭卷回答已经涵盖要点的候选直接丢弃,不入库;后台任务结果里记为「跳过 · 模型已会」。"],
      href: isAdmin ? "/admin/jobs" : "/enzymes",
      linkLabel: isAdmin ? "后台任务" : "记忆",
    }),
    step("gate", 3, LANE.learn, {
      kind: "condition",
      icon: <ShieldCheckIcon />,
      title: "质量门",
      detail: "5 个维度打质量分",
      metric: quality ? `≥ ${quality.pass} 通过 · < ${quality.quarantine} 拒绝` : "—",
      ...(caps?.llm === false
        ? { status: "warn" as const, statusLabel: caps.judge ? "判官打分" : "规则打分" }
        : { status: "ok" as const }),
      rules: [
        "先脱敏(去掉密钥、口令等),再按具体性、新颖性、准确性、通用性、可执行性各打 1~5 分,取平均。",
        quality
          ? `平均分 ≥ ${quality.pass} 通过;${quality.refine}~${quality.pass} 之间由语言模型改写后入库,没有改写稿就隔离;${quality.quarantine}~${quality.refine} 隔离待审;低于 ${quality.quarantine} 拒绝。`
          : "—",
        "打分顺序:语言模型 → 判官(只在语言模型失败时,打分题不写改写稿)→ 规则。",
      ],
      href: "/review",
      linkLabel: "审核",
    }),
    step("reject", 4, LANE.learn + 130, {
      kind: "action",
      icon: <BanIcon />,
      title: "不入库",
      detail: "质量分太低",
      metric: "返回改进建议",
      status: "idle",
      statusLabel: "丢弃",
      rules: [quality ? `平均分低于 ${quality.quarantine} 的不存,只返回改进建议。` : "—"],
      href: "/review",
      linkLabel: "审核",
    }),
    step("domain", 4, LANE.learn, {
      kind: "action",
      icon: <FolderIcon />,
      title: "领域归类",
      detail: domainAuto === false ? "已关闭:沿用提炼时给的领域名" : "归入已有领域,都不合适就新建",
      metric: domainCount === undefined ? "—" : `${formatNumber(domainCount)} 个领域`,
      ...(domainAuto === false ? toggle(false) : model(caps === undefined ? undefined : Boolean(caps.judge || caps.llm))),
      rules: [
        `判官一道单选:最适合收进哪个已有领域(选项是领域名和一句话描述),都不合适就选 none;最高项 ≥ ${num("domain.assign_q")} 且高于 none 才归入。`,
        `都不合适时由语言模型看着领域表新建一个大类;领域数到 ${num("domain.max_count")} 后不再新建,归入最接近的。`,
        `巩固时,成员不超过 ${num("domain.merge_below")} 条的小领域逐条重新归类,能并进大领域就并过去。`,
        "任何一步失败都沿用提炼时给的领域名,不影响入库。",
      ],
      href: "/enzymes",
      linkLabel: "按领域看记忆",
    }),
    step("store", 6, LANE.learn, {
      kind: "action",
      icon: <DatabaseIcon />,
      title: "入库启用",
      detail: "参与以后的召回",
      metric: `${n(dashboard?.by_status.active)} 条启用`,
      status: "ok",
      rules: [
        on("exemplar.learn") === false
          ? "语言模型抽出触发词,记忆写进向量索引和词面索引。"
          : "语言模型抽出触发词,记忆写进向量索引和词面索引;诞生时的情境记为第一条经验样本。",
        "起始置信度按质量分折算,最高 0.55,之后靠反馈升降。",
      ],
      href: "/enzymes",
      linkLabel: "记忆",
    }),
    step("quarantine", 5, LANE.learn + 110, {
      kind: "action",
      icon: <ShieldAlertIcon />,
      title: "隔离审核",
      detail: "人工确认后才启用",
      metric: `${n(home?.pending_review)} 条待审`,
      status: home ? (home.pending_review ? "warn" : "ok") : "idle",
      statusLabel: home ? (home.pending_review ? "待处理" : "已清空") : "读取中",
      rules: [
        "可疑的记忆先隔离(置信度 0.15),不参与召回,等人在审核页确认。",
        "确认就启用;拒绝的不参与召回。巩固时的免疫巡查也会把问题记忆送到这里。",
      ],
      href: "/review",
      linkLabel: "审核",
    }),

    // ── 巩固 ──
    step("schedule", 0, LANE.consolidate, {
      kind: "trigger",
      icon: <CalendarClockIcon />,
      title: "定时巩固",
      detail: schedule ? `每 ${duration(schedule.interval)}` : "后台按计划运行",
      metric: schedule?.running
        ? "正在运行"
        : home?.last_consolidation_at
          ? `上次 ${relativeTime(home.last_consolidation_at)}${schedule?.enabled && schedule.due_in > 0 ? ` · ${duration(schedule.due_in)}后` : ""}`
          : "还没运行过",
      ...(schedule
        ? schedule.enabled
          ? { status: "ok" as const, statusLabel: schedule.running ? "运行中" : undefined }
          : toggle(false)
        : { status: "idle" as const, statusLabel: "读取中" }),
      rules: [
        schedule
          ? `上次巩固完成后满 ${duration(schedule.interval)}再跑下一次;重启服务不会立刻重跑。`
          : "—",
        "也可以在巩固调度页手动执行;每一步单独容错,一步失败不影响其余步骤。",
      ],
      href: "/learning/consolidation",
      linkLabel: "巩固调度",
    }),
    step("cleanup", 1, LANE.consolidate, {
      kind: "action",
      icon: <TrendingDownIcon />,
      title: "衰减与清理",
      detail: "效果差的降权,不删除",
      metric: "从没用过的不衰减",
      status: "ok",
      rules: [
        "用过但效果差、90 天没再用上的记忆,置信度每次降 0.05;从没被用过的不衰减。",
        "触发 30 次以上、命中率不到 10% 的标为休眠(不删除)。",
        "试用 5 次以上、命中率低于 30% 的变异记忆清理掉。",
      ],
      href: "/learning/consolidation",
      linkLabel: "巩固调度",
    }),
    step("merge", 2, LANE.consolidate, {
      kind: "action",
      icon: <GitMergeIcon />,
      title: "合并去重",
      detail: "近重复的合并成一条",
      metric: `语义去重 ${onOff(dedup)}`,
      status: "ok",
      rules: [
        "触发词与建议字面高度重叠的,在同一领域内合并。",
        dedup === false
          ? "语义去重:已关闭。"
          : `语义去重:判官判定两条是否实质相同,概率 ≥ ${num("learning.dedup_tau")} 才合并;被吸收的记忆完整快照记进进化日志。`,
        `小领域归并:成员不超过 ${num("domain.merge_below")} 条的领域逐条重新归类。`,
      ],
      href: isAdmin ? "/settings/runtime" : "/learning/consolidation",
      linkLabel: isAdmin ? "运行参数" : "巩固调度",
    }),
    step("evolve", 3, LANE.consolidate, {
      kind: "action",
      icon: <MoonStarIcon />,
      title: "做梦与回放",
      detail: schedule
        ? [
            schedule.dreamglia_enabled ? "做梦、免疫巡查" : "",
            schedule.replay_enabled ? "情景回放" : "",
            schedule.mutation_enabled ? "突变" : "",
            schedule.crossover_enabled ? "交叉" : "",
          ]
            .filter(Boolean)
            .join("、") || "全部关闭"
        : "读取中",
      metric: schedule ? `突变 ${onOff(schedule.mutation_enabled)} · 交叉 ${onOff(schedule.crossover_enabled)}` : "—",
      ...toggle(
        schedule
          ? schedule.dreamglia_enabled || schedule.replay_enabled || schedule.mutation_enabled || schedule.crossover_enabled
          : undefined,
      ),
      rules: schedule
        ? [
            schedule.dreamglia_enabled
              ? `做梦:语言模型把已有记忆重组成新假设,每次最多 ${schedule.dreamglia_max_dreams} 条,经探索槽试用后优胜劣汰。`
              : "做梦:已关闭。",
            schedule.dreamglia_enabled
              ? "免疫巡查:把反复失败的、很久没用过的、和别的记忆建议相反的送去隔离。"
              : "免疫巡查:已关闭。",
            schedule.replay_enabled
              ? `情景回放:拿历史情景(每次最多 ${schedule.replay_max_episodes} 个)考现有记忆,给探索项和少有反馈的记忆判卷。`
              : "情景回放:已关闭。",
            `突变、交叉(实验性):${onOff(schedule.mutation_enabled)}、${onOff(schedule.crossover_enabled)}。`,
          ]
        : ["—"],
      href: "/learning/evolution",
      linkLabel: "进化日志",
    }),
    step("sync", 4, LANE.consolidate, {
      kind: "action",
      icon: <RefreshCwIcon />,
      title: "规则与索引",
      detail: "规则文件 + 全量同步",
      metric: "重算全部向量",
      status: "ok",
      rules: [
        "最成熟的 10 条记忆写进规则文件,给不走 hook 的工具读。",
        "全部记忆重新写进向量索引和词面索引(重算向量),保证索引和数据库一致。",
      ],
      href: "/learning/consolidation",
      linkLabel: "巩固调度",
    }),
  ]

  const edges: Edge[] = [
    edge("ask", "route", undefined, true),
    edge("route", "recall", routeMode === "strict" ? "只在选中领域" : routeMode === "expert" ? "补召回" : undefined, true),
    edge("recall", "rerank", undefined, true),
    edge("rerank", "judge", undefined, true),
    edge("judge", "inject", judgeOn === false ? "固定剂量" : "适用", true),
    edge("judge", "skip", "都不适用"),
    edge("inject", "feedback", undefined, true),
    edge("session", "extract", undefined, true),
    edge("extract", "known", "候选", true),
    edge("known", "gate", skipKnown === false ? "新经验" : "模型不会 / 新经验", true),
    edge("known", "known-skip", "模型已会"),
    edge("gate", "domain", "通过 / 可疑", true),
    edge("gate", "reject", "拒绝"),
    edge("domain", "store", "通过", true),
    edge("domain", "quarantine", "可疑"),
    edge("quarantine", "store", "确认"),
    edge("schedule", "cleanup", undefined, true),
    edge("cleanup", "merge", undefined, true),
    edge("merge", "evolve", undefined, true),
    edge("evolve", "sync", undefined, true),
  ]

  return { nodes, edges }
}
