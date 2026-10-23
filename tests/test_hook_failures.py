"""工具失败学习路径:capture digest 保留 [tool_error]/[tool_ok] 行,
PostToolUseFailure hook 在报错时注入旧经验(source="failure"),prime hook 输出不变。

hook 脚本不在包内,用 importlib 按路径加载(hooks/ 目录在 sys.path 以便其内部互引)。
"""
import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import ce_capture_hook  # noqa: E402
import ce_failure_hook  # noqa: E402
import ce_prime_hook  # noqa: E402

pytestmark = pytest.mark.asyncio


def _write_jsonl(tmp_path, objs) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs), encoding="utf-8")
    return p


def _run_hook(mod, payload, monkeypatch, response=None, boom=False):
    """跑一遍 hook main():喂 stdin JSON,替身 HTTP 层(_OPENER.open),收 stdout。返回 (stdout_str, [request])。

    hook 通过 sys.exit(0) 结束,捕获 SystemExit。open 的返回值要支持 with/read(n)。"""
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def fake_open(req, timeout=0):
        calls.append(req)
        if boom:
            raise RuntimeError("server down")
        return Resp(json.dumps(response or {"matches": []}).encode())

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", out)
    # hook 的 HTTP 走各自的 _OPENER(failure 复用 ce_prime_hook 的 post_json)
    opener = getattr(mod, "_OPENER", None) or ce_prime_hook._OPENER
    monkeypatch.setattr(opener, "open", fake_open)
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code == 0
    return out.getvalue(), calls


def _transcript_objs():
    inject_block = ("⟦CE:BEGIN⟧\n[认知酶 · System 1 直觉] 以下反射被当前上下文激活"
                    "，供参考（无关可忽略）：\n🔴 [90%] 先检查端口占用  ‹enzyme_id=old1›\n"
                    "若已连接 cognitive-enzyme MCP：验证后请调 report(enzyme_ids=[上列 id 按序], "
                    "success) 反馈以强化/纠正；任务中途需要更多相关经验可调 search(query) 主动检索。"
                    "\n⟦CE:END⟧")
    return [
        {"type": "user", "message": {"role": "user", "content": "帮我跑一下 npm test"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "先跑测试看看"},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "npm test"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
             "content": [{"type": "text",
                          "text": "Exit code 1\nError: Cannot find module 'express'"}]},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Read",
             "input": {"file_path": "/x/package.json"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "is_error": False,
             "content": "{\n  \"name\": \"app\"\n}"},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t3", "name": "Bash",
             "input": {"command": "npm install express && npm test"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t3", "is_error": False,
             "content": "added 1 package\nok 12 passed"},
        ]}},
        {"type": "attachment", "attachment": {"content": inject_block}},
    ]


async def test_digest_keeps_tool_errors_and_fixes(tmp_path):
    """摘要保留报错原文 + 后来的成功命令;普通 tool_result(Read)不出现。"""
    p = _write_jsonl(tmp_path, _transcript_objs())
    digest = ce_capture_hook._read_transcript(str(p))

    assert "[user] 帮我跑一下 npm test" in digest
    assert "[assistant] 先跑测试看看" in digest
    assert "[tool_error] Bash: npm test\nExit code 1\nError: Cannot find module 'express'" in digest
    assert "[tool_ok] Bash: npm install express && npm test" in digest
    assert "Read" not in digest and "package.json" not in digest   # 成功的非 Bash 工具不进摘要
    assert "⟦CE:SEEN old1⟧" in digest                             # 注入块照旧压缩


async def test_failure_hook_posts_and_injects(tmp_path, monkeypatch):
    """PostToolUseFailure:source=failure 发 /api/prime,命中 → 换抬头的 CE 块。"""
    payload = {
        "session_id": "s1", "transcript_path": str(tmp_path / "none.jsonl"),
        "cwd": "/tmp/proj", "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash", "tool_input": {"command": "npm test"},
        "tool_use_id": "t1",
        "error": "Exit code 1\nError: Cannot find module 'express'",
    }
    resp = {"matches": [{"id": "fix1", "confidence": 0.9, "severity": "critical",
                         "suggestion": "npm install express", "reason": "缺依赖"}],
            "total": 1}
    out, calls = _run_hook(ce_failure_hook, payload, monkeypatch, response=resp)

    assert len(calls) == 1
    req = calls[0]
    body = json.loads(req.data.decode())
    assert req.full_url.endswith("/api/prime")
    assert body["source"] == "failure" and body["max_results"] == 3
    assert body["session_id"] == "s1" and body["cwd"] == "/tmp/proj"
    assert "Bash" in body["context"] and "npm test" in body["context"]
    assert "Cannot find module 'express'" in body["context"]

    data = json.loads(out)
    assert data["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure"
    ctx = data["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("⟦CE:BEGIN⟧\n[认知酶 · 以前遇到类似报错的经验]")
    assert "‹enzyme_id=fix1›" in ctx and ctx.endswith("⟦CE:END⟧")


async def test_failure_hook_interrupt_and_down(tmp_path, monkeypatch):
    """is_interrupt / 服务不可用 → 无输出、静默退出 0。"""
    payload = {"tool_name": "Bash", "tool_input": {"command": "x"}, "error": "boom",
               "is_interrupt": True}
    out, calls = _run_hook(ce_failure_hook, payload, monkeypatch)
    assert out == "" and calls == []

    payload["is_interrupt"] = False
    out, calls = _run_hook(ce_failure_hook, payload, monkeypatch, boom=True)
    assert out == "" and len(calls) == 1

    payload.pop("error")                                        # 空 error 也不发请求
    out, calls = _run_hook(ce_failure_hook, payload, monkeypatch)
    assert out == "" and calls == []


async def test_prime_hook_output_unchanged(tmp_path, monkeypatch):
    """重构守卫:固定假响应下 prime hook 的输出逐字不变。"""
    payload = {"prompt": "端口被占用怎么办", "session_id": "s1",
               "transcript_path": str(tmp_path / "none.jsonl"), "cwd": ""}
    resp = {"matches": [{"id": "e1", "confidence": 0.9, "severity": "critical",
                         "suggestion": "先检查端口占用", "reason": "常见原因"},
                        {"id": "e2", "confidence": 0.5, "severity": "info",
                         "suggestion": "换个端口", "reason": ""},
                        {"id": "e3", "confidence": 0.1, "exploratory": True,
                         "suggestion": "试试重启路由", "reason": "假设"}],
            "total": 3}
    out, calls = _run_hook(ce_prime_hook, payload, monkeypatch, response=resp)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    expected = "\n".join([
        "⟦CE:BEGIN⟧",
        "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）：",
        "🔴 [90%] 先检查端口占用（因为：常见原因）  ‹enzyme_id=e1›",
        "🟢 [50%] 换个端口  ‹enzyme_id=e2›",
        "⚗️ [假设 10%] 试试重启路由（因为：假设）  ‹enzyme_id=e3›",
        "标 ⚗️ 者为记忆演化产生的未验证假设：可试可忽略，不构成约束；"
        "无论采用与否，若能判断其对错请如实 report。",
        "若已连接 cognitive-enzyme MCP：验证后请调 report(enzyme_ids=[上列 id 按序], success) "
        "反馈以强化/纠正；任务中途需要更多相关经验可调 search(query) 主动检索。",
        "⟦CE:END⟧",
    ])
    assert ctx == expected
    body = json.loads(calls[0].data.decode())
    assert "source" not in body or body["source"] == "prime"     # prime 请求不变
    assert body["project"] == "" and body["max_results"] == 6


async def test_prime_hook_marks_methodology_hint(tmp_path, monkeypatch):
    payload = {"prompt": "怎么检查这个推导", "session_id": "s1",
               "transcript_path": str(tmp_path / "none.jsonl"), "cwd": ""}
    resp = {"matches": [{"id": "g1", "confidence": 0.4, "severity": "info",
                         "scope": "general", "suggestion": "先做极限情形检查"}],
            "total": 1}
    out, _ = _run_hook(ce_prime_hook, payload, monkeypatch, response=resp)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "🧭 [方法论提示 40%] 先做极限情形检查" in ctx
    assert "标 🧭 者为通用方法论提示：可尝试、可忽略，不是事实断言。" in ctx


async def test_prime_route_passes_source(monkeypatch):
    """/api/prime:source='failure' 透传给 engine.match,其他值归一为 'prime'。"""
    from app.routers import operations
    from app.schemas import PrimeRequest

    seen = {}

    class FakeEngine:
        pg = type("PG", (), {})()

        async def match(self, context, domain=None, max_results=15, features=None,
                        project="", session_id="", source="prime", **kw):
            seen["source"] = source
            return []

        async def record_session_context(self, sid, ctx):
            pass

    async def _refractory(pg, session_id, results, seen_ids=None):
        return results

    async def _mark(pg, session_id, ids):
        pass

    monkeypatch.setattr(operations, "filter_refractory", _refractory)
    monkeypatch.setattr(operations, "mark_injections", _mark)
    eng = FakeEngine()

    await operations.prime(PrimeRequest(context="x", source="failure"), engine=eng)
    assert seen["source"] == "failure"
    await operations.prime(PrimeRequest(context="x"), engine=eng)
    assert seen["source"] == "prime"
    await operations.prime(PrimeRequest(context="x", source="bogus"), engine=eng)
    assert seen["source"] == "prime"
