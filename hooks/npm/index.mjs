#!/usr/bin/env node
/**
 * cognitive-enzyme-hook — Claude Code `UserPromptSubmit` hook.
 *
 * 把 Cognitive Enzyme 的反射作为 System 1 直觉注入当前 prompt：读取 Claude Code
 * 在 stdin 给的 hook 输入（含 prompt），调 CE 的 REST `/api/prime`，把命中的酶
 * 格式化成一段上下文，通过 additionalContext 注回模型。
 *
 * 设计原则：**绝不阻塞 prompt**。CE 没起、超时、报错——一律静默退出 0、不注入。
 * 零依赖：仅用 Node 18+ 内置 fetch / AbortController / process.stdin。
 *
 * 可配置 env：
 *   CE_API_URL        REST 基址，默认 http://localhost:8420
 *   CE_API_TOKEN      静态机器令牌（服务端开启鉴权时必填）
 *   CE_PRIME_TIMEOUT  请求超时（秒），默认 9（服务端判官准确度优先，需低于 hook 的 10s 超时）
 *   CE_PRIME_MAX      注入条数上限，默认 6
 *   CE_PRIME_MIN_CONF 置信度下限，默认 0.25
 *   CE_DOMAIN         可选。限定召回领域（如 'python'）
 *   CE_PROJECT        可选。显式项目标识（容器里 cwd 恒为 /workspace 没有
 *                     信息量时用它；本机场景服务端会从 cwd 自行推导）
 */

import { emit, formatBlock, readStdin, seenIds } from "./common.mjs";

const API_URL = (process.env.CE_API_URL || "http://localhost:8420").replace(/\/+$/, "");
const API_TOKEN = (process.env.CE_API_TOKEN || "").trim();
const TIMEOUT_MS = Math.round((Number(process.env.CE_PRIME_TIMEOUT) || 9) * 1000);
const MAX_RESULTS = Number(process.env.CE_PRIME_MAX) || 6;
// 出厂值与 python hook(hooks/ce_prime_hook.py)和评测 harness 对齐:0.25
// (2026-07 召回调优定标;0.4 会闷死 0.3~0.4 置信度的新酶)
const MIN_CONFIDENCE = Number(process.env.CE_PRIME_MIN_CONF) || 0.25;
const DOMAIN = (process.env.CE_DOMAIN || "").trim();
const PROJECT = (process.env.CE_PROJECT || "").trim();

const HEADER = "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）：";

async function main() {
  if (typeof fetch !== "function") emit(""); // Node <18：放弃注入，不报错

  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    emit("");
  }

  const context = String(payload.prompt || "").trim();
  if (!context) emit("");

  const headers = { "Content-Type": "application/json" };
  if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let data;
  try {
    const res = await fetch(`${API_URL}/api/prime`, {
      method: "POST",
      headers,
      // session_id → 服务端注入不应期(同酶同会话窗口内不重复注入);
      // seen_ids → 上下文里已经有的注入,不管过了多久都不再重复;
      // cwd → 服务端扫描技术栈特征,加厚情境表征。都是尽力而为,缺了也能跑。
      body: JSON.stringify({
        context,
        max_results: MAX_RESULTS,
        session_id: String(payload.session_id || ""),
        seen_ids: seenIds(String(payload.transcript_path || "")),
        cwd: String(payload.cwd || ""),
        domain: DOMAIN,
        project: PROJECT, // 项目维度:同项目酶加权召回(空则服务端从 cwd 推导)
      }),
      signal: controller.signal,
    });
    if (!res.ok) emit(""); // 401 / 5xx 等：静默放过
    data = await res.json();
  } catch {
    emit(""); // 服务不可用 / 超时
  } finally {
    clearTimeout(timer);
  }

  // exploratory = 服务端探索槽注入的未验证假设,豁免置信度过滤(否则永无验证机会)
  const matches = (data.matches || []).filter(
    (m) => (m.confidence || 0) >= MIN_CONFIDENCE || m.exploratory,
  );
  if (matches.length === 0) emit("");

  // ⟦CE:BEGIN⟧/⟦CE:END⟧ 哨兵供 capture 端识别注入块:剥离防回声 + 被动效果评估
  emit(formatBlock(matches, HEADER));
}

main().catch(() => process.exit(0));
