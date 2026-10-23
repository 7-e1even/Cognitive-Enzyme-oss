"""被动闭环单测 — 纯 mock,无需起服务/连库。

直接跑: python tests/test_passive_eval.py
覆盖:注入痕迹剥离(哨兵块/SEEN 行/旧格式)、防回声(注入文本不进 extract)、
     helped/contradicted/neutral 三路信号、(session_id, enzyme_id) 幂等去重。
"""
import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import Enzyme, Pattern  # noqa: E402
from app.learning import capture  # noqa: E402


# ── Fakes ──────────────────────────────────────────────────────

class FakePG:
    def __init__(self, evaluated=None):
        # {(session_id, enzyme_id): verdict} — 镜像 capture_evals 契约:
        # neutral 非终局,可被后续 helped/contradicted 覆盖升级
        self.evals = {key: "helped" for key in (evaluated or [])}
        self.recorded = []

    async def filter_unevaluated(self, session_id, enzyme_ids):
        return [e for e in enzyme_ids
                if self.evals.get((session_id, e), "neutral") == "neutral"]

    async def record_eval(self, session_id, enzyme_id, verdict):
        key = (session_id, enzyme_id)
        if self.evals.get(key, "neutral") == "neutral":
            self.evals[key] = verdict
        self.recorded.append((session_id, enzyme_id, verdict))

    async def log_event(self, event):
        pass


class FakeEngine:
    def __init__(self, enzymes=None, evaluated=None):
        self._enzymes = enzymes or {}
        self.pg = FakePG(evaluated)
        self.hebbian_calls = []

    async def get_enzyme(self, eid):
        return self._enzymes.get(eid)

    async def hebbian_update(self, enzyme_id, success, expected=None, detail=""):
        self.hebbian_calls.append((enzyme_id, success, detail))
        return {"enzyme_id": enzyme_id}


def _enzyme(eid):
    return Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"建议{eid}",
                  because="理由", confidence=0.6)


def _patch_llm(verdicts):
    """评估提示(含「判定标准」)返回 verdicts;其余(extract)返回空。"""
    async def fake_chat_json(system, user, config=None):
        if "判定标准" in user:
            return verdicts
        return []

    async def fake_embed(text, config=None):
        return None

    capture.chat_json = fake_chat_json
    capture.embed = fake_embed


INJECTED = (
    "[user] ⟦CE:BEGIN⟧\n"
    "[认知酶 · System 1 直觉] 以下反射被当前上下文激活，供参考（无关可忽略）：\n"
    "🟢 [80%] 建议A  ‹enzyme_id=e1›\n"
    "🟡 [60%] 建议B  ‹enzyme_id=e2›\n"
    "若已连接 cognitive-enzyme MCP：验证后请调 report 反馈。\n"
    "⟦CE:END⟧\n"
    "帮我修这个 bug\n\n"
    "[assistant] 按建议A处理了,问题解决。"
)


# ── 剥离与收集 ──────────────────────────────────────────────────

def test_split_sentinel_block():
    clean, ids = capture.split_injections(INJECTED)
    assert ids == ["e1", "e2"], ids
    assert "⟦CE:BEGIN⟧" not in clean and "‹enzyme_id" not in clean
    assert "帮我修这个 bug" in clean and "按建议A处理了" in clean


def test_split_seen_line():
    clean, ids = capture.split_injections("[user] ⟦CE:SEEN e3,e4⟧\n继续干活")
    assert ids == ["e3", "e4"], ids
    assert "SEEN" not in clean and "继续干活" in clean


def test_split_legacy_block():
    text = ("[user] [认知酶 · System 1 直觉] 以下反射被激活：\n"
            "🟢 [70%] 老建议  ‹enzyme_id=e9›\n"
            "采纳并验证某条反射后，可调 report(enzyme_id, success) 反馈以强化/纠正它。\n"
            "真正的问题在这里")
    clean, ids = capture.split_injections(text)
    assert ids == ["e9"], ids
    assert "老建议" not in clean and "真正的问题在这里" in clean


def test_split_plain_text_untouched():
    text = "普通对话,没有任何注入痕迹"
    clean, ids = capture.split_injections(text)
    assert clean == text and ids == []


# ── 防回声 ──────────────────────────────────────────────────────

async def test_echo_stripped_from_extract():
    eng = FakeEngine(enzymes={"e1": _enzyme("e1"), "e2": _enzyme("e2")})
    seen = {}

    async def fake_chat_json(system, user, config=None):
        if "判定标准" in user:
            return []
        seen["extract_prompt"] = user
        return []

    async def fake_embed(text, config=None):
        return None

    capture.chat_json = fake_chat_json
    capture.embed = fake_embed

    await capture.capture_session(eng, INJECTED, session_id="s0")
    assert "‹enzyme_id" not in seen["extract_prompt"]   # 注入文本绝不进提炼
    assert "帮我修这个 bug" in seen["extract_prompt"]


# ── 三路信号 ────────────────────────────────────────────────────

async def test_eval_three_verdicts():
    eng = FakeEngine(enzymes={"e1": _enzyme("e1"), "e2": _enzyme("e2"),
                              "e3": _enzyme("e3")})
    _patch_llm([
        {"id": "e1", "verdict": "helped", "evidence": "按建议A处理了"},
        {"id": "e2", "verdict": "contradicted", "evidence": "用户说不对"},
        {"id": "e3", "verdict": "neutral", "evidence": ""},
    ])
    text = INJECTED + "\n[user] ⟦CE:SEEN e3⟧"
    summary = await capture.capture_session(eng, text, session_id="s1")

    assert summary["passive_helped"] == 1, summary
    assert summary["passive_contradicted"] == 1, summary
    assert summary["passive_neutral"] == 1, summary
    outcomes = {(c[0], c[1]) for c in eng.hebbian_calls}
    assert ("e1", True) in outcomes and ("e2", False) in outcomes
    assert all(c[0] != "e3" for c in eng.hebbian_calls)   # neutral 不惩罚不奖励
    assert {("s1", "e1", "helped"), ("s1", "e2", "contradicted"),
            ("s1", "e3", "neutral")} <= set(eng.pg.recorded)


async def test_eval_idempotent():
    """同会话已评估过的酶不再评估:PreCompact 多次触发不重复计分。"""
    eng = FakeEngine(enzymes={"e1": _enzyme("e1"), "e2": _enzyme("e2")},
                     evaluated=[("s1", "e1"), ("s1", "e2")])
    calls = {"eval": 0}

    async def fake_chat_json(system, user, config=None):
        if "判定标准" in user:
            calls["eval"] += 1
        return []

    async def fake_embed(text, config=None):
        return None

    capture.chat_json = fake_chat_json
    capture.embed = fake_embed

    await capture.capture_session(eng, INJECTED, session_id="s1")
    assert calls["eval"] == 0        # 全部已评估 → 连 LLM 都不调
    assert eng.hebbian_calls == []


async def test_eval_neutral_upgrades_to_helped():
    """PreCompact 记 neutral 的酶不被锁定:SessionEnd 再评可升级为 helped。"""
    eng = FakeEngine(enzymes={"e1": _enzyme("e1"), "e2": _enzyme("e2")})
    # 第一轮(模拟 PreCompact):e1 尚未被用到 → neutral;e2 → helped
    _patch_llm([
        {"id": "e1", "verdict": "neutral", "evidence": ""},
        {"id": "e2", "verdict": "helped", "evidence": "按建议B处理"},
    ])
    await capture.capture_session(eng, INJECTED, session_id="s3")
    assert eng.pg.evals[("s3", "e1")] == "neutral"

    # 第二轮(模拟 SessionEnd):只有 e1 待重评,升级为 helped;e2 已终局不再送评
    _patch_llm([{"id": "e1", "verdict": "helped", "evidence": "后来按建议A解决"}])
    summary = await capture.capture_session(eng, INJECTED, session_id="s3")
    assert summary["passive_helped"] == 1, summary
    assert eng.pg.evals[("s3", "e1")] == "helped"
    ids = [c[0] for c in eng.hebbian_calls]
    assert ids.count("e1") == 1 and ids.count("e2") == 1   # 各只计分一次


async def test_eval_unknown_id_ignored():
    """LLM 幻觉出的 id、库里已不存在的酶,一律忽略。"""
    eng = FakeEngine(enzymes={"e1": _enzyme("e1")})   # e2 不存在
    _patch_llm([
        {"id": "hallucinated", "verdict": "helped", "evidence": "x"},
        {"id": "e1", "verdict": "helped", "evidence": "y"},
    ])
    summary = await capture.capture_session(eng, INJECTED, session_id="s2")
    assert summary["passive_helped"] == 1, summary
    assert [c[0] for c in eng.hebbian_calls] == ["e1"]


# ── Runner ─────────────────────────────────────────────────────

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if inspect.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"[ok] {name}")
        except AssertionError as e:
            failed += 1
            print(f"[fail] {name}: {e}")
    if failed:
        sys.exit(f"{failed} test(s) failed")
    print("all passive-eval tests passed")
