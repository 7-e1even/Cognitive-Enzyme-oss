#!/usr/bin/env python3
"""SessionEnd / PreCompact / Stop hook：把本次会话提炼成记忆,异步写回 CE。

与 prime hook(读路径)相对,这是 System 2 写路径:会话结束或上下文被压缩前,
读取 Claude Code 的 transcript,POST 到 CE 的 /api/capture。CE 端会异步做
「提取候选 → 撞库 → 对账(ADD/UPDATE/REINFORCE/INVALIDATE)」,本 hook 立即返回。

设计原则:**绝不阻塞、绝不报错给用户**。CE 没起/超时/异常一律静默退出 0。
只用标准库。挂在 Stop 上会每轮触发,建议优先挂 SessionEnd + PreCompact。
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import deque

API_URL = os.environ.get("CE_API_URL", "http://localhost:8420").rstrip("/")
API_TOKEN = os.environ.get("CE_API_TOKEN", "").strip()


def _env_int(name: str, default: int) -> int:
    """数值环境变量坏值回落默认——hook 承诺 fail-open,配置写错也不抛错退出。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


TIMEOUT = _env_float("CE_CAPTURE_TIMEOUT", 5.0)               # 端点立即返回,故可短
MAX_CHARS = _env_int("CE_CAPTURE_MAX_CHARS", 200000)   # transcript 摘要上限(服务端再按运行参数截一次)
MAX_TURNS = _env_int("CE_CAPTURE_TURNS", 400)          # 最多纳入多少轮
MAX_TURN_CHARS = _env_int("CE_CAPTURE_TURN_CHARS", 20000)  # 单轮上限:一条巨型 tool_result 不独占摘要预算
PROJECT = os.environ.get("CE_PROJECT", "").strip()             # 显式项目标识(容器场景;缺省服务端从 cwd 推)


def _bail():
    sys.exit(0)


# urllib 默认重定向会把 Authorization 带给任意主机(307/308):跨主机剥掉,防令牌外泄
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


# prime 注入块(哨兵定界或旧格式)压缩为一行 ⟦CE:SEEN id,...⟧:
# 省摘要预算,同时保住酶 id 供服务端做被动效果评估。
_INJECT_RE = re.compile(r"⟦CE:BEGIN⟧.*?⟦CE:END⟧", re.DOTALL)
_LEGACY_RE = re.compile(r"\[认知酶 · System 1 直觉\].*?(?:主动检索。|以强化/纠正它。)", re.DOTALL)
_ID_RE = re.compile(r"‹enzyme_id=([^›\s]+)›")


def _compress_injections(text: str) -> str:
    def repl(m):
        ids = list(dict.fromkeys(_ID_RE.findall(m.group(0))))
        return f"⟦CE:SEEN {','.join(ids)}⟧" if ids else ""
    return _LEGACY_RE.sub(repl, _INJECT_RE.sub(repl, text))


def _iter_capped(f) -> list:
    """内存有界地读 transcript:开头 100 行 + 尾部 4000 行,单行截 200k 字符。
    摘要用的是开头(交代任务)与结尾(怎么修好的),中段可丢;几百 MB 的
    transcript 不再全量驻留内存(readlines 全量加载曾把 SessionEnd 卡住)。"""
    head: list[str] = []
    tail: deque = deque(maxlen=4000)
    for i, line in enumerate(f):
        line = line[:200_000]
        if i < 100:
            head.append(line)
        else:
            tail.append(line)
    return head + list(tail)


def _text_from_content(content):
    """Claude Code message.content 可能是字符串或 block 列表,统一抽成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _tool_summary(tool_input) -> str:
    """tool_use 的 input 压成一行摘要:命令 > 文件路径 > 整个 JSON(failure hook 也用它)。"""
    if not isinstance(tool_input, dict):
        tool_input = {}
    s = tool_input.get("command") or tool_input.get("file_path") \
        or json.dumps(tool_input, ensure_ascii=False)
    return " ".join(str(s).split())[:200]


def _read_transcript(path: str) -> str:
    """读取 transcript JSONL,拼出最近若干轮 user/assistant 的纯文本。

    除文本轮外保留工具的真实执行痕迹:tool_result is_error → [tool_error] 行
    (工具名:命令摘要 + 报错原文,截 800 字);之后成功执行的 Bash → [tool_ok] 行。
    「报错 → 后来修好」的片段是"怎么修"经验最可靠的原料。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = _iter_capped(f)
    except Exception:
        return ""

    tools = {}   # tool_use_id → (name, summary)
    turns = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "attachment":
            # hook 注入存成 attachment 记录而非消息:只留 CE 注入块(压成 SEEN 行),其余附件不要
            att = obj.get("attachment") or {}
            body = att.get("content")
            body = "\n".join(body) if isinstance(body, list) else body
            if isinstance(body, str) and ("⟦CE:BEGIN⟧" in body or "‹enzyme_id=" in body):
                seen = _compress_injections(body).strip()
                if seen:
                    turns.append(f"[user] {seen}")
            continue
        msg = obj.get("message") or obj
        role = msg.get("role") or obj.get("type")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if role == "assistant" and block.get("type") == "tool_use":
                    tools[block.get("id")] = (block.get("name") or "tool",
                                              _tool_summary(block.get("input")))
                elif role == "user" and block.get("type") == "tool_result":
                    name, summary = tools.get(block.get("tool_use_id"), ("tool", ""))
                    if block.get("is_error"):
                        err = _text_from_content(block.get("content", ""))
                        turns.append(f"[tool_error] {name}: {summary}\n{err[:800]}")
                    elif name == "Bash":
                        turns.append(f"[tool_ok] Bash: {summary[:150]}")
        text = _compress_injections(_text_from_content(content)).strip()
        if text:
            turns.append(f"[{role}] {text[:MAX_TURN_CHARS]}")

    if not turns:
        return ""
    # 超出上限时保留开头(交代任务是什么)和大部分结尾(最后是怎么解决的)
    if len(turns) > MAX_TURNS:
        head = min(2, MAX_TURNS // 2)
        turns = turns[:head] + turns[len(turns) - (MAX_TURNS - head):]
    digest = "\n\n".join(turns)
    if len(digest) > MAX_CHARS:
        head = MAX_CHARS // 10
        digest = digest[:head] + "\n\n…(中间省略)…\n\n" + digest[len(digest) - (MAX_CHARS - head):]
    return digest


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _bail()
    if not isinstance(payload, dict):
        _bail()

    transcript_path = payload.get("transcript_path", "")
    if not transcript_path:
        _bail()

    transcript = _read_transcript(transcript_path)
    if not transcript or len(transcript) < 200:   # 太短不值得跑 LLM
        _bail()

    source = (payload.get("hook_event_name") or "").lower() or "capture"
    body = json.dumps({
        "transcript": transcript,
        "session_id": payload.get("session_id", ""),
        "source": source,
        "domain": "",
        "cwd": str(payload.get("cwd") or ""),
        "project": PROJECT,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    req = urllib.request.Request(
        f"{API_URL}/api/capture", data=body, headers=headers, method="POST",
    )
    try:
        _OPENER.open(req, timeout=TIMEOUT).read(65536)  # 响应体有上限,慢滴不拖住 hook
    except Exception:
        pass  # 服务不可用 → 静默放过
    _bail()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _bail()  # hook 永不阻塞:任何意外都静默退出 0
