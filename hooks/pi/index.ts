/**
 * cognitive-enzyme — pi 扩展:与 Claude Code hooks(ce_prime_hook /
 * ce_failure_hook / ce_capture_hook,hooks/npm/*)同一套 CE 集成。
 *
 * 三件事:
 *   1. before_agent_start → POST /api/prime,命中的酶以 ⟦CE:BEGIN⟧…⟦CE:END⟧
 *      块作为 custom_message(customType="cognitive-enzyme") 注入上下文。
 *   2. tool_result isError → 同 /api/prime(source="failure"),命中的"以前遇到
 *      类似报错的经验"追加在报错内容后面。
 *   3. session_before_compact / session_shutdown → 把分支摘要(含
 *      [tool_error]/[tool_ok] 工具痕迹)POST /api/capture,增量捕获。
 *
 * 设计原则与 hooks 相同:**绝不阻塞**——CE 没起、超时、报错一律静默,不注入、
 * 不抛错。env 与 hooks 同名同默认;CE_PI_DISABLE=1 时什么都不注册。
 */

import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { buildDigest, formatBlock, makeConfig, parseEnvFile, seenIds, toolSummary } from "./core.mjs";

// hook.env 文件兜底:服务端开了鉴权时把静态机器令牌放这里,env 优先。
const FILE_ENV = (() => {
  try {
    return parseEnvFile(
      readFileSync(join(homedir(), ".config/cognitive-enzyme/hook.env"), "utf8"),
    );
  } catch {
    return {};
  }
})();
const cfg = makeConfig(FILE_ENV);

const API_URL = cfg("CE_API_URL", "http://localhost:8420").replace(/\/+$/, "");
const API_TOKEN = cfg("CE_API_TOKEN", "").trim();
const PRIME_TIMEOUT_MS = Math.round(Number(cfg("CE_PRIME_TIMEOUT", "9")) * 1000 || 9000);
const PRIME_MAX = Number(cfg("CE_PRIME_MAX", "6")) || 6;
const MIN_CONFIDENCE = Number(cfg("CE_PRIME_MIN_CONF", "0.25")) || 0.25;
const FAILURE_MAX = Number(cfg("CE_FAILURE_MAX", "3")) || 3;
const FAILURE_TIMEOUT_MS = Math.round(Number(cfg("CE_FAILURE_TIMEOUT", "9")) * 1000 || 9000);
const CAPTURE_TIMEOUT_MS = Math.round(Number(cfg("CE_CAPTURE_TIMEOUT", "5")) * 1000 || 5000);
const DOMAIN = cfg("CE_DOMAIN", "").trim();
const PROJECT = cfg("CE_PROJECT", "").trim();

const CUSTOM_TYPE = "cognitive-enzyme";
const PRIME_HEADER = "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）：";
const FAILURE_HEADER = "[认知酶 · 以前遇到类似报错的经验] 以下经验被这次报错激活，供参考（无关可忽略）：";
const MIN_DIGEST_CHARS = 200;

/** POST JSON,超时/网络错/非 2xx → null;绝不抛。 */
async function post(path: string, body: unknown, timeoutMs: number): Promise<any> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${API_URL}${path}`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// exploratory = 服务端探索槽注入的未验证假设,豁免置信度过滤(否则永无验证机会)
function filterMatches(data: any): any[] {
  const matches = data && Array.isArray(data.matches) ? data.matches : [];
  return matches.filter((m: any) => (m.confidence || 0) >= MIN_CONFIDENCE || m.exploratory);
}

function sessionId(ctx: any): string {
  try {
    return String(ctx.sessionManager.getSessionId() || "");
  } catch {
    return "";
  }
}

function branch(ctx: any): any[] {
  try {
    return ctx.sessionManager.getBranch() || [];
  } catch {
    return [];
  }
}

export default function (pi: any) {
  if (cfg("CE_PI_DISABLE", "") === "1") return;
  if (typeof fetch !== "function") return; // Node <18:放弃注入,不报错

  // 本 runtime 已捕获到哪个条目(增量捕获;compaction 会换掉 branch,找不到时全量)
  let lastCapturedId: string | null = null;

  async function capture(ctx: any, source: string, entries?: any[]): Promise<void> {
    const all = entries || branch(ctx);
    let slice = all;
    if (lastCapturedId) {
      const idx = all.findIndex((e: any) => e && e.id === lastCapturedId);
      if (idx >= 0) slice = all.slice(idx + 1);
    }
    if (slice.length === 0) return;
    const digest = buildDigest(slice);
    if (!digest || digest.length < MIN_DIGEST_CHARS) return;
    const tail = slice[slice.length - 1];
    const data = await post(
      "/api/capture",
      {
        transcript: digest,
        session_id: sessionId(ctx),
        source,
        domain: "",
        cwd: ctx.cwd || "",
        project: PROJECT,
      },
      CAPTURE_TIMEOUT_MS,
    );
    if (data !== null && tail && tail.id) lastCapturedId = tail.id;
  }

  pi.on("before_agent_start", async (event: any, ctx: any) => {
    try {
      const context = String(event.prompt || "").trim();
      if (!context) return;
      const data = await post(
        "/api/prime",
        {
          context,
          max_results: PRIME_MAX,
          session_id: sessionId(ctx),
          seen_ids: seenIds(branch(ctx)),
          cwd: ctx.cwd || "",
          domain: DOMAIN,
          project: PROJECT,
        },
        PRIME_TIMEOUT_MS,
      );
      const matches = filterMatches(data);
      if (matches.length === 0) return;
      return {
        message: {
          customType: CUSTOM_TYPE,
          content: formatBlock(matches, PRIME_HEADER),
          display: false,
        },
      };
    } catch {
      return;
    }
  });

  pi.on("tool_result", async (event: any, ctx: any) => {
    try {
      if (!event.isError) return;
      const errText = (event.content || [])
        .filter((c: any) => c && c.type === "text")
        .map((c: any) => c.text || "")
        .join("\n")
        .trim();
      if (!errText) return;
      const context =
        `[工具报错] ${event.toolName || "tool"}: ${toolSummary(event.input)}\n` +
        errText.slice(0, 1500);
      const data = await post(
        "/api/prime",
        {
          context,
          max_results: FAILURE_MAX,
          session_id: sessionId(ctx),
          seen_ids: seenIds(branch(ctx)),
          cwd: ctx.cwd || "",
          domain: DOMAIN,
          project: PROJECT,
          source: "failure",
        },
        FAILURE_TIMEOUT_MS,
      );
      const matches = filterMatches(data);
      if (matches.length === 0) return;
      return {
        content: [
          ...(event.content || []),
          { type: "text", text: "\n\n" + formatBlock(matches, FAILURE_HEADER) },
        ],
      };
    } catch {
      return;
    }
  });

  pi.on("session_before_compact", async (event: any, ctx: any) => {
    try {
      await capture(ctx, "precompact", event.branchEntries);
    } catch {}
  });

  // pi -p 打印模式下 session_shutdown 也会触发(实测 v0.87.1),无需 agent_settled 兜底
  pi.on("session_shutdown", async (_event: any, ctx: any) => {
    try {
      await capture(ctx, "sessionend");
    } catch {}
  });
}
