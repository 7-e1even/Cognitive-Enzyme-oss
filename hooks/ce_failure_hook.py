#!/usr/bin/env python3
"""PostToolUseFailure hook：工具真实执行失败时,注入"上次修好同类报错"的经验。

Claude Code 在工具报错时触发本 hook(stdin JSON 带 tool_name / tool_input / error)。
把报错压成情境发给 /api/prime(source="failure"),命中的酶以与 prime 注入相同的
⟦CE:BEGIN⟧…⟦CE:END⟧ 块注回模型,只是抬头写明"以前遇到类似报错的经验"。

设计原则与 prime hook 相同:**绝不阻塞**——CE 没起、超时、报错一律静默退出 0。
只用标准库,避免依赖项目 venv。

可配置 env:
  CE_API_URL           REST 基址,默认 http://localhost:8420
  CE_API_TOKEN         静态机器令牌(服务端开启鉴权时必填)
  CE_FAILURE_TIMEOUT   请求超时(秒),默认 9
  CE_FAILURE_MAX       注入条数上限,默认 3
  CE_PRIME_MIN_CONF    置信度下限,默认 0.25(与 prime 共用)
  CE_DOMAIN / CE_PROJECT  可选:限定召回领域 / 显式项目标识
"""
import json
import os
import sys

try:
    from ce_capture_hook import _tool_summary
    from ce_prime_hook import _env_float, _env_int, emit, format_block, post_json, seen_ids
except Exception:
    sys.exit(0)   # 同目录共享模块缺失时也不阻塞

API_URL = os.environ.get("CE_API_URL", "http://localhost:8420").rstrip("/")
API_TOKEN = os.environ.get("CE_API_TOKEN", "").strip()
TIMEOUT = _env_float("CE_FAILURE_TIMEOUT", 9.0)
MAX_RESULTS = _env_int("CE_FAILURE_MAX", 3)
MIN_CONFIDENCE = _env_float("CE_PRIME_MIN_CONF", 0.25)
DOMAIN = os.environ.get("CE_DOMAIN", "").strip()
PROJECT = os.environ.get("CE_PROJECT", "").strip()

HEADER = "[认知酶 · 以前遇到类似报错的经验] 以下经验被这次报错激活，供参考（无关可忽略）："


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit("", "PostToolUseFailure")
    if not isinstance(payload, dict):
        emit("", "PostToolUseFailure")

    if payload.get("is_interrupt"):
        emit("", "PostToolUseFailure")
    error = str(payload.get("error") or "").strip()
    if not error:
        emit("", "PostToolUseFailure")

    tool_name = str(payload.get("tool_name") or "tool")
    summary = _tool_summary(payload.get("tool_input"))
    context = f"[工具报错] {tool_name}: {summary}\n{error[:1500]}"

    body = json.dumps({
        "context": context,
        "max_results": MAX_RESULTS,
        "session_id": str(payload.get("session_id") or ""),
        "seen_ids": seen_ids(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "domain": DOMAIN,
        "project": PROJECT,
        "source": "failure",
    }).encode()
    data = post_json("/api/prime", body, TIMEOUT)
    if data is None:
        emit("", "PostToolUseFailure")  # 服务不可用 → 静默放过

    # exploratory = 服务端探索槽注入的未验证假设,豁免置信度过滤(否则永无验证机会)
    raw_matches = data.get("matches") or []
    matches = [m for m in raw_matches if isinstance(m, dict)
               and (m.get("confidence", 0) >= MIN_CONFIDENCE or m.get("exploratory"))]
    if not matches:
        emit("", "PostToolUseFailure")

    emit(format_block(matches, HEADER), "PostToolUseFailure")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)   # hook 永不阻塞:任何意外都静默
