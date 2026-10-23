"""注入不应期(Refractory, PG 版)单测 — FakePG,无需连库。

直接跑: python tests/test_refractory.py
覆盖:窗口内抑制、critical 豁免、无 session 放行、PG 故障放行(fail-open)、
     mark best-effort、prime 请求模型新字段。
SQL 层(recent_injections/record_injections)由真库集成脚本覆盖。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import Enzyme, Pattern, Severity  # noqa: E402
from app.delivery.refractory import filter_refractory, mark_injections  # noqa: E402


class _R:
    """模拟 MatchResult:只需要 .enzyme。"""
    def __init__(self, eid, severity=Severity.INFO):
        self.enzyme = Enzyme(id=eid, patterns=[Pattern(value="x")], then="t",
                             severity=severity)


class FakePG:
    """内存版 (session, enzyme) 注入记录。"""
    def __init__(self, recent=None, broken=False):
        self._recent = set(recent or [])
        self._broken = broken
        self.marked = []

    async def recent_injections(self, session_id, enzyme_ids, window_seconds):
        if self._broken:
            raise RuntimeError("pg down")
        return {e for e in enzyme_ids if e in self._recent}

    async def record_injections(self, session_id, enzyme_ids):
        if self._broken:
            raise RuntimeError("pg down")
        self.marked.append((session_id, list(enzyme_ids)))


async def test_suppress_recent():
    pg = FakePG(recent={"e1"})
    out = await filter_refractory(pg, "s1", [_R("e1"), _R("e2")])
    assert [r.enzyme.id for r in out] == ["e2"]
    print("[ok] suppress_recent")


async def test_critical_exempt():
    pg = FakePG(recent={"e1"})
    out = await filter_refractory(pg, "s1", [_R("e1", Severity.CRITICAL)])
    assert len(out) == 1                       # 真警报永远放行
    print("[ok] critical_exempt")


async def test_no_session_passthrough():
    pg = FakePG(recent={"e1"})
    out = await filter_refractory(pg, "", [_R("e1")])
    assert len(out) == 1
    print("[ok] no_session_passthrough")


async def test_pg_failure_fail_open():
    """不应期失效时宁可重复注入,绝不阻断 prime。"""
    pg = FakePG(broken=True)
    out = await filter_refractory(pg, "s1", [_R("e1")])
    assert len(out) == 1
    await mark_injections(pg, "s1", ["e1"])    # 不抛
    print("[ok] pg_failure_fail_open")


async def test_mark_records():
    pg = FakePG()
    await mark_injections(pg, "s1", ["e1", "e2"])
    await mark_injections(pg, "", ["e3"])      # 空 session 不记录
    await mark_injections(pg, "s1", [])        # 空列表不记录
    assert pg.marked == [("s1", ["e1", "e2"])], pg.marked
    print("[ok] mark_records")


async def test_seen_ids_suppress():
    """上下文里已可见的注入:不看时间窗、不需要 session_id,critical 仍放行。"""
    pg = FakePG()
    out = await filter_refractory(pg, "", [_R("e1"), _R("e2"), _R("e3", Severity.CRITICAL)],
                                  seen_ids=["e1", "e3"])
    assert [r.enzyme.id for r in out] == ["e2", "e3"], [r.enzyme.id for r in out]
    print("[ok] seen_ids_suppress")


def test_hook_seen_ids():
    """prime hook 从 transcript 解析已注入 id:只看最近一次压缩之后。"""
    import importlib.util
    import json
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "ce_prime_hook", Path(__file__).resolve().parent.parent / "hooks" / "ce_prime_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    def inject(*ids):
        body = "\n".join(f"🟢 [50%] tip  ‹enzyme_id={i}›" for i in ids)
        return {"type": "attachment", "attachment": {
            "type": "hook_additional_context", "hookEvent": "UserPromptSubmit",
            "content": [f"⟦CE:BEGIN⟧\n{body}\n⟦CE:END⟧"]}}

    rows = [
        inject("old"),
        {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"},
        inject("a", "b"),
        {"type": "user", "message": {"role": "user", "content": "提到 ‹enzyme_id=noise› 但不在注入块里"}},
        {"type": "user", "message": {"role": "user", "content": 'grep 输出 "compact_boundary"'}},
        inject("b", "c"),
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    assert sorted(hook.seen_ids(f.name)) == ["a", "b", "c"], hook.seen_ids(f.name)
    assert hook.seen_ids("/nonexistent.jsonl") == []
    assert hook.seen_ids("") == []
    print("[ok] hook_seen_ids")


def test_capture_hook_keeps_injection_attachments():
    """capture hook:注入存成 attachment 记录时,压成 SEEN 行保留;其他附件丢弃。"""
    import importlib.util
    import json
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "ce_capture_hook", Path(__file__).resolve().parent.parent / "hooks" / "ce_capture_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "修一下这个 bug"}},
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [
            "⟦CE:BEGIN⟧\n🟢 [50%] tip ‹enzyme_id=e1›\n🟢 [50%] tip2 ‹enzyme_id=e2›\n⟦CE:END⟧"]}},
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "content": ["别的 hook 的输出"]}},
        {"type": "assistant", "message": {"role": "assistant", "content": "好的"}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    out = hook._read_transcript(f.name)
    assert "⟦CE:SEEN e1,e2⟧" in out and "别的 hook" not in out and "tip2" not in out, out
    from app.learning.capture import split_injections
    clean, ids = split_injections(out)
    assert ids == ["e1", "e2"] and "CE:SEEN" not in clean, (ids, clean)
    print("[ok] capture_hook_keeps_injection_attachments")


def test_prime_request_fields():
    from app.schemas import PrimeRequest
    req = PrimeRequest(context="c", session_id="s1", cwd="/tmp/proj", domain="py", seen_ids=["e1"])
    assert req.session_id == "s1" and req.cwd == "/tmp/proj" and req.seen_ids == ["e1"]
    req2 = PrimeRequest(context="c")           # 旧调用方不传也兼容
    assert req2.session_id == "" and req2.cwd == "" and req2.seen_ids == []
    print("[ok] prime_request_fields")


async def main():
    print("=" * 50)
    print("注入不应期单测(PG 版)")
    print("=" * 50)
    await test_suppress_recent()
    await test_critical_exempt()
    await test_no_session_passthrough()
    await test_pg_failure_fail_open()
    await test_mark_records()
    await test_seen_ids_suppress()
    test_hook_seen_ids()
    test_capture_hook_keeps_injection_attachments()
    test_prime_request_fields()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    asyncio.run(main())
