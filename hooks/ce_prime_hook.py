#!/usr/bin/env python3
"""UserPromptSubmit hook：把 Cognitive-Enzyme 的反射作为 System 1 直觉注入当前 prompt。

读取 Claude Code 在 stdin 给的 hook 输入（含 prompt / cwd），调 CE 的
/api/prime，把命中的酶格式化成一段上下文，通过 additionalContext 注回模型。

设计原则：**绝不阻塞 prompt**。CE 没起、超时、报错——一律静默退出 0、不注入。
只用标准库，避免依赖项目 venv。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

API_URL = os.environ.get("CE_API_URL", "http://localhost:8420").rstrip("/")
API_TOKEN = os.environ.get("CE_API_TOKEN", "").strip()        # 静态机器令牌（鉴权开启时必填）


def _env_float(name: str, default: float) -> float:
    """数值环境变量:坏值回落默认——hook 承诺 fail-open,配置写错也不能抛错退出。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 秒。服务端 Stage 4.5 两位判官(Jev ≤6s,可选 LLM ≤8s)并行,准确度优先;
# 仍须低于 Claude Code 的 hook 超时(安装默认 10s),超时即静默不注入
TIMEOUT = _env_float("CE_PRIME_TIMEOUT", 9.0)
MAX_RESULTS = _env_int("CE_PRIME_MAX", 6)                      # 注入上限，控 token
MIN_CONFIDENCE = _env_float("CE_PRIME_MIN_CONF", 0.25)
DOMAIN = os.environ.get("CE_DOMAIN", "").strip()               # 可选:限定召回领域
PROJECT = os.environ.get("CE_PROJECT", "").strip()             # 可选:显式项目标识(容器场景)

SEV_ICON = {"critical": "🔴", "warning": "🟡", "info": "🟢"}
# 注入块首行:failure hook(工具报错时注入)复用同一格式,只换这句抬头
HEADER = "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）："

# transcript 是 JSONL,换行已转义成 \n,所以一个注入块总在同一行内
# (与 hooks/npm/index.mjs 的 seenIds 保持一致)
_BLOCK_RE = re.compile(r"⟦CE:BEGIN⟧.*?⟦CE:END⟧")
_ID_RE = re.compile(r"‹enzyme_id=([^›\s\\\"]+)›")


def seen_ids(transcript_path: str) -> list:
    """当前上下文里还看得见的注入:transcript 最近一次压缩之后的注入块里的酶 id。

    逐行流式读(transcript 可能几百 MB,readlines 全量加载会卡 SessionEnd);
    每遇到一个压缩边界就清空累计,最终剩下的就是最后一次压缩之后的注入。
    """
    if not transcript_path:
        return []
    ids = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                if '"compact_boundary"' in line:
                    try:
                        if json.loads(line).get("subtype") == "compact_boundary":
                            ids = []
                            continue
                    except Exception:
                        pass
                for block in _BLOCK_RE.findall(line):
                    ids += _ID_RE.findall(block)
    except Exception:
        return []
    return list(dict.fromkeys(ids))


def emit(additional_context: str, event_name: str = "UserPromptSubmit") -> None:
    """输出 hook 的 JSON；空串则不注入。"""
    if additional_context:
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            },
            "suppressOutput": True,
        }, sys.stdout)
    sys.exit(0)


# urllib 默认重定向会把 Authorization 头带给任意主机(307/308):跨主机时剥掉,防令牌外泄
class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(new.full_url).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_SafeRedirect())
_MAX_RESP_BYTES = 1_000_000  # 响应上限 1MB:异常的大响应/慢滴不拖累 hook


def post_json(path: str, body: bytes, timeout: float) -> dict | None:
    """POST JSON 并解析响应;任何失败(连不上/超时/非 JSON/非对象)返回 None。"""
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(f"{API_URL}{path}", data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read(_MAX_RESP_BYTES + 1))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def format_block(matches: list, header: str = HEADER) -> str:
    """把命中酶格式化成 ⟦CE:BEGIN⟧…⟦CE:END⟧ 注入块(failure hook 复用,只换抬头)。"""
    lines = ["⟦CE:BEGIN⟧", header]
    has_explorer = False
    for m in matches:
        conf = m.get("confidence", 0)
        is_general = m.get("scope") == "general"
        if m.get("exploratory"):
            has_explorer = True
        if is_general:
            icon, tag = "🧭", f"[方法论提示 {conf:.0%}]"
        elif m.get("exploratory"):
            icon, tag = "⚗️", f"[假设 {conf:.0%}]"
        else:
            icon, tag = SEV_ICON.get(m.get("severity", "info"), "⚪"), f"[{conf:.0%}]"
        line = f"{icon} {tag} {m.get('suggestion', '')}"
        if m.get("reason"):
            line += f"（因为：{m['reason']}）"
        line += f"  ‹enzyme_id={m.get('id')}›"
        lines.append(line)
    if has_explorer:
        lines.append(
            "标 ⚗️ 者为记忆演化产生的未验证假设：可试可忽略，不构成约束；"
            "无论采用与否，若能判断其对错请如实 report。"
        )
    if any(m.get("scope") == "general" for m in matches):
        lines.append("标 🧭 者为通用方法论提示：可尝试、可忽略，不是事实断言。")
    lines.append(
        "若已连接 cognitive-enzyme MCP：验证后请调 report(enzyme_ids=[上列 id 按序], success) "
        "反馈以强化/纠正；任务中途需要更多相关经验可调 search(query) 主动检索。"
    )
    lines.append("⟦CE:END⟧")
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit("")
    if not isinstance(payload, dict):
        emit("")

    context = (payload.get("prompt") or "").strip()
    if not context:
        emit("")

    # session_id → 服务端注入不应期(同酶同会话窗口内不重复注入);
    # seen_ids → 上下文里已经有的注入,不管过了多久都不再重复;
    # cwd → 服务端扫描技术栈特征,加厚情境表征。都是尽力而为,缺了也能跑。
    body = json.dumps({
        "context": context,
        "max_results": MAX_RESULTS,
        "session_id": str(payload.get("session_id") or ""),
        "seen_ids": seen_ids(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "domain": DOMAIN,
        "project": PROJECT,      # 项目维度:同项目酶加权召回(空则服务端从 cwd 推导)
    }).encode()
    data = post_json("/api/prime", body, TIMEOUT)
    if data is None:
        emit("")  # 服务不可用 → 静默放过

    # exploratory = 服务端探索槽注入的未验证假设,豁免置信度过滤(否则永无验证机会)
    raw_matches = data.get("matches") or []
    matches = [m for m in raw_matches if isinstance(m, dict)
               and (m.get("confidence", 0) >= MIN_CONFIDENCE or m.get("exploratory"))]
    if not matches:
        emit("")

    # ⟦CE:BEGIN⟧/⟦CE:END⟧ 哨兵供 capture 端识别注入块:剥离防回声 + 被动效果评估
    emit(format_block(matches))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # hook 永不阻塞:任何意外都静默(emit 走 sys.exit,不受此 catch 影响)
