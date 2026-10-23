/**
 * cognitive-enzyme hooks 共享件 — prime(index.mjs)、capture(capture.mjs)、
 * failure(failure.mjs)共用的 stdin 读取、注入块格式化、transcript 摘要与
 * seen_ids / tool_use 摘要。行为与各 hook 原有实现逐字一致。
 */

import { closeSync, fstatSync, openSync, readSync } from "node:fs";

export async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

/** 输出 hook 的 JSON 并退出；空串=不注入。永远 exit 0。 */
export function emit(additionalContext, hookEventName = "UserPromptSubmit") {
  if (additionalContext) {
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName,
          additionalContext,
        },
        suppressOutput: true,
      }),
    );
  }
  process.exit(0);
}

const SEV_ICON = { critical: "🔴", warning: "🟡", info: "🟢" };

/** 把命中酶格式化成 ⟦CE:BEGIN⟧…⟦CE:END⟧ 注入块(failure hook 复用,只换抬头)。 */
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

// transcript 是 JSONL,换行已转义成 \n,所以一个注入块总在同一行内
const BLOCK_RE = /⟦CE:BEGIN⟧.*?⟦CE:END⟧/g;
const SEEN_ID_RE = /‹enzyme_id=([^›\s\\"]+)›/g;

// transcript 可能几百 MB,全量 readFileSync 会把 hook 卡死:小文件照旧全读,
// 大文件只读尾部(seenIds 只关心最近一次压缩之后,压缩边界几乎必然在尾部窗口里)。
const SEEN_TAIL_BYTES = 6 * 1024 * 1024;
const MAX_WHOLE_FILE = 8 * 1024 * 1024;

/** 读文件尾部 bytes;从半截行切入时丢弃第一个不完整的行。 */
function readTail(path, bytes) {
  let fd;
  try {
    fd = openSync(path, "r");
    const size = fstatSync(fd).size;
    const start = Math.max(0, size - bytes);
    const buf = Buffer.alloc(size - start);
    readSync(fd, buf, 0, buf.length, start);
    let text = buf.toString("utf8");
    if (start > 0) {
      const nl = text.indexOf("\n");
      text = nl >= 0 ? text.slice(nl + 1) : "";
    }
    return text;
  } catch {
    return "";
  } finally {
    if (fd !== undefined) {
      try { closeSync(fd); } catch {}
    }
  }
}

/** 当前上下文里还看得见的注入:transcript 最近一次压缩之后的注入块里的酶 id。 */
export function seenIds(transcriptPath) {
  const raw = readTail(transcriptPath, SEEN_TAIL_BYTES);
  if (!raw) return [];
  const ids = [];
  const lines = raw.split("\n");
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (line.includes('"compact_boundary"')) {
      try {
        if (JSON.parse(line).subtype === "compact_boundary") break;
      } catch {}
    }
    for (const block of line.match(BLOCK_RE) || []) {
      for (const m of block.matchAll(SEEN_ID_RE)) ids.push(m[1]);
    }
  }
  return [...new Set(ids)];
}

/** tool_use 的 input 压成一行摘要:命令 > 文件路径 > 整个 JSON(failure hook 也用它)。 */
export function toolSummary(toolInput) {
  const input = toolInput && typeof toolInput === "object" ? toolInput : {};
  const s = input.command || input.file_path || JSON.stringify(input);
  return s.replace(/\s+/g, " ").slice(0, 200);
}

// prime 注入块(哨兵定界或旧格式)压缩为一行 ⟦CE:SEEN id,...⟧:
// 省摘要预算,同时保住酶 id 供服务端做被动效果评估。
const INJECT_RE = /⟦CE:BEGIN⟧[\s\S]*?⟦CE:END⟧/g;
const LEGACY_RE = /\[认知酶 · System 1 直觉\][\s\S]*?(?:主动检索。|以强化\/纠正它。)/g;
const ID_RE = /‹enzyme_id=([^›\s]+)›/g;

function compressInjections(text) {
  const repl = (m) => {
    const ids = [...new Set([...m.matchAll(ID_RE)].map((x) => x[1]))];
    return ids.length ? `⟦CE:SEEN ${ids.join(",")}⟧` : "";
  };
  return text.replace(INJECT_RE, repl).replace(LEGACY_RE, repl);
}

/** message.content 可能是字符串或 block 列表,统一抽成文本。 */
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

/**
 * 读取 transcript JSONL,拼出最近若干轮 user/assistant 的纯文本。
 * 除文本轮外保留工具的真实执行痕迹:tool_result is_error → [tool_error] 行
 * (工具名:命令摘要 + 报错原文,截 800 字);之后成功执行的 Bash → [tool_ok] 行。
 * 「报错 → 后来修好」的片段是"怎么修"经验最可靠的原料。
 *
 * 内存有界:小文件全读;大文件只读头 1MB(交代任务)+ 尾 6MB(最后怎么解决的)。
 */
const HEAD_BYTES = 1 * 1024 * 1024;
const TAIL_BYTES = 6 * 1024 * 1024;
const MAX_TURN_CHARS = 20000; // 单轮上限:一条巨型 tool_result 不独占摘要预算

function readHeadAndTail(path) {
  let fd;
  try {
    fd = openSync(path, "r");
    const size = fstatSync(fd).size;
    if (size <= MAX_WHOLE_FILE) {
      const buf = Buffer.alloc(size);
      readSync(fd, buf, 0, size, 0);
      return buf.toString("utf8");
    }
    const head = Buffer.alloc(HEAD_BYTES);
    readSync(fd, head, 0, HEAD_BYTES, 0);
    const tail = Buffer.alloc(TAIL_BYTES);
    readSync(fd, tail, 0, TAIL_BYTES, size - TAIL_BYTES);
    let tailText = tail.toString("utf8");
    const nl = tailText.indexOf("\n"); // 尾部从文件中段切开,丢掉半截的首行
    if (nl >= 0) tailText = tailText.slice(nl + 1);
    return head.toString("utf8") + "\n" + tailText;
  } catch {
    return "";
  } finally {
    if (fd !== undefined) {
      try { closeSync(fd); } catch {}
    }
  }
}

export function readTranscript(path, maxTurns, maxChars) {
  const raw = readHeadAndTail(path);
  if (!raw) return "";
  const tools = {}; // tool_use_id → { name, summary }
  const turns = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let obj;
    try {
      obj = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (obj.type === "attachment") {
      // hook 注入存成 attachment 记录而非消息:只留 CE 注入块(压成 SEEN 行),其余附件不要
      let body = (obj.attachment || {}).content;
      if (Array.isArray(body)) body = body.join("\n");
      if (typeof body === "string" && (body.includes("⟦CE:BEGIN⟧") || body.includes("‹enzyme_id="))) {
        const seen = compressInjections(body).trim();
        if (seen) turns.push(`[user] ${seen}`);
      }
      continue;
    }
    const msg = obj.message || obj;
    const role = msg.role || obj.type;
    if (role !== "user" && role !== "assistant") continue;
    const content = msg.content || "";
    if (Array.isArray(content)) {
      for (const b of content) {
        if (!b || typeof b !== "object") continue;
        if (role === "assistant" && b.type === "tool_use") {
          tools[b.id] = { name: b.name || "tool", summary: toolSummary(b.input) };
        } else if (role === "user" && b.type === "tool_result") {
          const t = tools[b.tool_use_id] || { name: "tool", summary: "" };
          if (b.is_error) {
            const err = textFromContent(b.content || "");
            turns.push(`[tool_error] ${t.name}: ${t.summary}\n${err.slice(0, 800)}`);
          } else if (t.name === "Bash") {
            turns.push(`[tool_ok] Bash: ${t.summary.slice(0, 150)}`);
          }
        }
      }
    }
    const text = compressInjections(textFromContent(content)).trim();
    if (text) turns.push(`[${role}] ${text.slice(0, MAX_TURN_CHARS)}`);
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
