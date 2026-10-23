/**
 * cognitive-enzyme pi 扩展的纯函数件 — 与 hooks/npm/common.mjs 行为逐字一致,
 * 只是输入从 Claude Code transcript JSONL 换成 pi SessionEntry 列表
 * (ctx.sessionManager.getBranch())。零依赖,供 node --test 单测。
 */

/**
 * 解析 KEY=VALUE 行(忽略空行与 # 注释,值可带单/双引号,等号两侧空白随意)。
 * 供 ~/.config/cognitive-enzyme/hook.env 这类免安装配置文件用。
 */
export function parseEnvFile(text) {
  const out = {};
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if (
      val.length >= 2 &&
      ((val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'")))
    ) {
      val = val.slice(1, -1);
    }
    if (key) out[key] = val;
  }
  return out;
}

/** 配置读取器:process.env > 文件 > 默认值(env 显式设置即胜出)。 */
export function makeConfig(fileEnv, env = process.env) {
  const file = fileEnv || {};
  return (key, fallback) => env[key] ?? file[key] ?? fallback;
}

const SEV_ICON = { critical: "🔴", warning: "🟡", info: "🟢" };

/** 把命中酶格式化成 ⟦CE:BEGIN⟧…⟦CE:END⟧ 注入块(与 hooks/npm/common.mjs 逐字一致)。 */
export function formatBlock(matches, header) {
  const lines = ["⟦CE:BEGIN⟧", header];
  let hasExplorer = false;
  for (const m of matches) {
    const conf = Math.round((m.confidence || 0) * 100);
    const isGeneral = m.scope === "general";
    let icon, tag;
    if (m.exploratory) hasExplorer = true;
    if (isGeneral) {
      icon = "🧭";
      tag = `[方法论提示 ${conf}%]`;
    } else if (m.exploratory) {
      icon = "⚗️";
      tag = `[假设 ${conf}%]`;
    } else {
      icon = SEV_ICON[m.severity] || "⚪";
      tag = `[${conf}%]`;
    }
    let line = `${icon} ${tag} ${m.suggestion || ""}`;
    if (m.reason) line += `（因为：${m.reason}）`;
    line += `  ‹enzyme_id=${m.id}›`;
    lines.push(line);
  }
  if (hasExplorer) {
    lines.push(
      "标 ⚗️ 者为记忆演化产生的未验证假设：可试可忽略，不构成约束；" +
        "无论采用与否，若能判断其对错请如实 report。",
    );
  }
  if (matches.some((m) => m.scope === "general")) {
    lines.push("标 🧭 者为通用方法论提示：可尝试、可忽略，不是事实断言。");
  }
  lines.push(
    "若已连接 cognitive-enzyme MCP：验证后请调 report(enzyme_ids=[上列 id 按序], success) " +
      "反馈以强化/纠正；任务中途需要更多相关经验可调 search(query) 主动检索。",
  );
  lines.push("⟦CE:END⟧");
  return lines.join("\n");
}

const BLOCK_RE = /⟦CE:BEGIN⟧[\s\S]*?⟦CE:END⟧/g;
const SEEN_ID_RE = /‹enzyme_id=([^›\s]+)›/g;
const CUSTOM_TYPE = "cognitive-enzyme";

/** content 可能是字符串或 block 列表,统一抽成文本。 */
function textFromContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((b) => b && b.type === "text")
      .map((b) => b.text || "")
      .join("\n");
  }
  return "";
}

/** tool_call 的 arguments 压成一行摘要:命令 > 文件路径 > 整个 JSON。 */
export function toolSummary(input) {
  const args = input && typeof input === "object" ? input : {};
  const s = args.command || args.file_path || args.path || JSON.stringify(args);
  return s.replace(/\s+/g, " ").slice(0, 200);
}

/**
 * 当前上下文里还看得见的注入:分支上最近一次 compaction 之后,
 * 我们的 custom_message 注入块和 toolResult 内追加块里的酶 id。
 */
export function seenIds(entries) {
  const list = Array.isArray(entries) ? entries : [];
  let start = 0;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i] && list[i].type === "compaction") {
      start = i + 1;
      break;
    }
  }
  const ids = [];
  for (const e of list.slice(start)) {
    if (!e || typeof e !== "object") continue;
    let text = "";
    if (e.type === "custom_message" && e.customType === CUSTOM_TYPE) {
      text = textFromContent(e.content);
    } else if (e.type === "message" && e.message && e.message.role === "toolResult") {
      text = textFromContent(e.message.content);
    }
    if (!text.includes("⟦CE:BEGIN⟧")) continue;
    for (const block of text.match(BLOCK_RE) || []) {
      for (const m of block.matchAll(SEEN_ID_RE)) ids.push(m[1]);
    }
  }
  return [...new Set(ids)];
}

const INJECT_RE = /⟦CE:BEGIN⟧[\s\S]*?⟦CE:END⟧/g;

/** 注入块压缩为一行 ⟦CE:SEEN id,...⟧:省摘要预算 + 保住酶 id 供被动效果评估。 */
function compressInjections(text) {
  return text.replace(INJECT_RE, (m) => {
    const ids = [...new Set([...m.matchAll(SEEN_ID_RE)].map((x) => x[1]))];
    return ids.length ? `⟦CE:SEEN ${ids.join(",")}⟧` : "";
  });
}

/**
 * 把 pi session 分支条目拼成 capture 用的纯文本摘要,与
 * hooks/npm/common.mjs readTranscript 的产出形态一致:
 *   [user] / [assistant] 文本轮(跳过 thinking);
 *   toolResult isError → [tool_error] {tool}: {摘要}\n{报错≤800字};
 *   成功的 bash toolResult → [tool_ok] bash: {摘要≤150字};
 *   我们的 custom_message / 文本内注入块 → ⟦CE:SEEN …⟧;
 *   compaction → [user] (之前对话的摘要) {summary≤2000字}。
 */
export function buildDigest(entries, maxTurns = 400, maxChars = 200000) {
  const list = Array.isArray(entries) ? entries : [];
  const tools = {}; // toolCallId → { name, summary }
  const turns = [];
  for (const e of list) {
    if (!e || typeof e !== "object") continue;
    if (e.type === "compaction") {
      const summary = String(e.summary || "").trim();
      if (summary) turns.push(`[user] (之前对话的摘要) ${summary.slice(0, 2000)}`);
      continue;
    }
    if (e.type === "custom_message") {
      if (e.customType !== CUSTOM_TYPE) continue;
      const seen = compressInjections(textFromContent(e.content)).trim();
      if (seen) turns.push(`[user] ${seen}`);
      continue;
    }
    if (e.type !== "message") continue;
    const msg = e.message || {};
    const role = msg.role;
    if (role === "toolResult") {
      const t = tools[msg.toolCallId] || { summary: "" };
      const name = msg.toolName || t.name || "tool";
      if (msg.isError) {
        const err = compressInjections(textFromContent(msg.content || "")).trim();
        turns.push(`[tool_error] ${name}: ${t.summary}\n${err.slice(0, 800)}`);
      } else if (name === "bash") {
        turns.push(`[tool_ok] bash: ${t.summary.slice(0, 150)}`);
      }
      continue;
    }
    const content = msg.content || "";
    if (role === "assistant" && Array.isArray(content)) {
      for (const b of content) {
        if (b && b.type === "toolCall") {
          tools[b.id] = {
            name: b.name || "tool",
            summary: toolSummary(b.arguments),
          };
        }
      }
    }
    if (role !== "user" && role !== "assistant") continue;
    const text = compressInjections(textFromContent(content)).trim();
    if (text) turns.push(`[${role}] ${text}`);
  }
  if (turns.length === 0) return "";
  // 超出上限时保留开头(交代任务是什么)和大部分结尾(最后是怎么解决的)
  let kept = turns;
  if (turns.length > maxTurns) {
    const head = Math.min(2, Math.floor(maxTurns / 2));
    kept = turns.slice(0, head).concat(turns.slice(turns.length - (maxTurns - head)));
  }
  let digest = kept.join("\n\n");
  if (digest.length > maxChars) {
    const head = Math.floor(maxChars / 10);
    digest = digest.slice(0, head) + "\n\n…(中间省略)…\n\n" + digest.slice(digest.length - (maxChars - head));
  }
  return digest;
}
