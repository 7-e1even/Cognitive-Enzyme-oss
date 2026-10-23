#!/usr/bin/env node
/**
 * cognitive-enzyme-capture — Claude Code `SessionEnd` / `PreCompact` / `Stop` hook.
 *
 * System 2 写路径(与 index.mjs 读路径相对):会话结束或上下文压缩前,读取
 * Claude Code 的 transcript,POST 到 CE 的 REST `/api/capture`。CE 端异步做
 * 「提取候选 → 撞库 → 对账(ADD/UPDATE/REINFORCE/INVALIDATE)」,本 hook 立即返回。
 *
 * transcript 摘要除文本轮外还保留工具的真实执行痕迹(见 common.mjs):
 * tool_result is_error → [tool_error] 行;之后成功的 Bash → [tool_ok] 行。
 *
 * 设计原则:**绝不阻塞、绝不报错给用户**。CE 没起/超时/异常一律静默退出 0。
 * 零依赖:仅用 Node 18+ 内置 fetch / AbortController / fs。
 *
 * 可配置 env:
 *   CE_API_URL          REST 基址,默认 http://localhost:8420
 *   CE_API_TOKEN        静态机器令牌(服务端开启鉴权时必填)
 *   CE_CAPTURE_TIMEOUT  请求超时(秒),默认 5
 *   CE_CAPTURE_MAX_CHARS transcript 摘要上限,默认 200000(服务端再按运行参数截一次)
 *   CE_CAPTURE_TURNS    最多纳入多少轮,默认 400
 *   CE_PROJECT          可选。显式项目标识(容器场景),否则服务端从 cwd 推导
 */

import { readStdin, readTranscript } from "./common.mjs";

const API_URL = (process.env.CE_API_URL || "http://localhost:8420").replace(/\/+$/, "");
const API_TOKEN = (process.env.CE_API_TOKEN || "").trim();
const TIMEOUT_MS = Math.round((Number(process.env.CE_CAPTURE_TIMEOUT) || 5) * 1000);
const MAX_CHARS = Number(process.env.CE_CAPTURE_MAX_CHARS) || 200000;
const MAX_TURNS = Number(process.env.CE_CAPTURE_TURNS) || 400;

function bail() {
  process.exit(0);
}

async function main() {
  if (typeof fetch !== "function") bail(); // Node <18

  let payload;
  try {
    payload = JSON.parse(await readStdin());
  } catch {
    bail();
  }

  const transcriptPath = payload.transcript_path || "";
  if (!transcriptPath) bail();

  const transcript = readTranscript(transcriptPath, MAX_TURNS, MAX_CHARS);
  if (!transcript || transcript.length < 200) bail(); // 太短不值得跑 LLM

  const headers = { "Content-Type": "application/json" };
  if (API_TOKEN) headers.Authorization = `Bearer ${API_TOKEN}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    await fetch(`${API_URL}/api/capture`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        transcript,
        session_id: payload.session_id || "",
        source: (payload.hook_event_name || "capture").toLowerCase(),
        domain: "",
        // 项目维度:新生酶记项目出身;CE_PROJECT 显式覆盖(容器场景),否则服务端从 cwd 推导
        cwd: String(payload.cwd || ""),
        project: (process.env.CE_PROJECT || "").trim(),
      }),
      signal: controller.signal,
    });
  } catch {
    // 服务不可用 / 超时 → 静默放过
  } finally {
    clearTimeout(timer);
  }
  bail();
}

main().catch(() => process.exit(0));
