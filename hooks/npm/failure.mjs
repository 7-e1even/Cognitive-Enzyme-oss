#!/usr/bin/env node
/**
 * cognitive-enzyme-failure — Claude Code `PostToolUseFailure` hook.
 *
 * 工具真实执行失败时触发:把报错压成情境发给 CE 的 REST `/api/prime`
 * (source="failure"),命中的酶以与 prime 注入相同的 ⟦CE:BEGIN⟧…⟦CE:END⟧ 块
 * 注回模型,只是抬头写明"以前遇到类似报错的经验"。
 *
 * 设计原则与 prime hook 相同:**绝不阻塞**——CE 没起、超时、报错一律静默退出 0。
 * 零依赖:仅用 Node 18+ 内置 fetch / AbortController / process.stdin。
 *
 * 可配置 env:
 *   CE_API_URL          REST 基址,默认 http://localhost:8420
 *   CE_API_TOKEN        静态机器令牌(服务端开启鉴权时必填)
 *   CE_FAILURE_TIMEOUT  请求超时(秒),默认 9
 *   CE_FAILURE_MAX      注入条数上限,默认 3
 *   CE_PRIME_MIN_CONF   置信度下限,默认 0.25(与 prime 共用)
 *   CE_DOMAIN           可选。限定召回领域
 *   CE_PROJECT          可选。显式项目标识(容器场景)
 */

import { emit, formatBlock, readStdin, seenIds, toolSummary } from "./common.mjs";

const API_URL = (process.env.CE_API_URL || "http://localhost:8420").replace(/\/+$/, "");
const API_TOKEN = (process.env.CE_API_TOKEN || "").trim();
const TIMEOUT_MS = Math.round((Number(process.env.CE_FAILURE_TIMEOUT) || 9) * 1000);
const MAX_RESULTS = Number(process.env.CE_FAILURE_MAX) || 3;
const MIN_CONFIDENCE = Number(process.env.CE_PRIME_MIN_CONF) || 0.25;
const DOMAIN = (process.env.CE_DOMAIN || "").trim();
const PROJECT = (process.env.CE_PROJECT || "").trim();

const HEADER = "[认知酶 · 以前遇到类似报错的经验] 以下经验被这次报错激活，供参考（无关可忽略）：";

async function main() {
  if (typeof fetch !== "function") emit("", "PostToolUseFailure"); // Node <18

  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    emit("", "PostToolUseFailure");
  }

  if (payload.is_interrupt) emit("", "PostToolUseFailure");
  const error = String(payload.error || "").trim();
  if (!error) emit("", "PostToolUseFailure");

  const toolName = String(payload.tool_name || "tool");
  const summary = toolSummary(payload.tool_input);
  const context = `[工具报错] ${toolName}: ${summary}\n${error.slice(0, 1500)}`;

  const headers = { "Content-Type": "application/json" };
  if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let data;
  try {
    const res = await fetch(`${API_URL}/api/prime`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        context,
        max_results: MAX_RESULTS,
        session_id: String(payload.session_id || ""),
        seen_ids: seenIds(String(payload.transcript_path || "")),
        cwd: String(payload.cwd || ""),
        domain: DOMAIN,
        project: PROJECT,
        source: "failure",
      }),
      signal: controller.signal,
    });
    if (!res.ok) emit("", "PostToolUseFailure");
    data = await res.json();
  } catch {
    emit("", "PostToolUseFailure");
  } finally {
    clearTimeout(timer);
  }

  const matches = (data.matches || []).filter(
    (m) => (m.confidence || 0) >= MIN_CONFIDENCE || m.exploratory,
  );
  if (matches.length === 0) emit("", "PostToolUseFailure");

  emit(formatBlock(matches, HEADER), "PostToolUseFailure");
}

main().catch(() => process.exit(0));
