"""情景回放测试单测 — FakeEngine/FakePG,无需连库连 LLM。

直接跑: python tests/test_replay.py
覆盖:考题不含答案、防自证(enzyme_ids + 出处指针)、判卷计分(credit=0.5、
     neutral 不惩罚)、幂等终局、LLM 输出容错、整轮编排与单幕失败隔离。
SQL 层(replay_evals 表)由真库集成脚本覆盖。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import Enzyme, Episode, EventType, Pattern  # noqa: E402
from app.learning import replay  # noqa: E402


# ── 假件 ───────────────────────────────────────────────────────

class _R:
    """模拟 MatchResult:回放只用 .enzyme。"""
    def __init__(self, enzyme):
        self.enzyme = enzyme


def _enzyme(eid, references=None):
    return Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}",
                  because=f"because-{eid}", references=references or [])


def _episode(eid="ep1", enzyme_ids=None, **kw):
    fields = dict(
        title="修复 JWT 刷新令牌死循环",
        intent="让令牌过期后自动续期",
        events=["刷新接口 401", "发现拦截器重复注册"],
        entities=["auth.ts", "axios"],
        error_signatures=["401 Unauthorized loop"],
        root_cause="拦截器在热重载后重复注册",
        outcome="去重注册后解决",
        lesson="拦截器注册要幂等",
        excerpt="console: 401 401 401 ...",
        salience=0.8,
    )
    fields.update(kw)
    return Episode(id=eid, enzyme_ids=enzyme_ids or [], **fields)


class FakePG:
    def __init__(self, episodes=None, replayed=None):
        self._episodes = episodes or []
        self.replayed = set(replayed or [])       # {(episode_id, enzyme_id)}
        self.recorded = []                        # [(episode_id, enzyme_id, verdict)]
        self.events = []

    async def replay_candidates(self, limit=5, min_salience=0.5):
        return self._episodes[:limit]

    async def filter_unreplayed(self, episode_id, enzyme_ids):
        return [e for e in enzyme_ids if (episode_id, e) not in self.replayed]

    async def record_replay_eval(self, episode_id, enzyme_id, verdict):
        self.replayed.add((episode_id, enzyme_id))
        self.recorded.append((episode_id, enzyme_id, verdict))

    async def log_event(self, event):
        self.events.append(event)


class FakeEngine:
    def __init__(self, matches=None, episodes=None, replayed=None, match_error=False):
        self.pg = FakePG(episodes=episodes, replayed=replayed)
        self._matches = matches or []
        self._match_error = match_error
        self.hebbian_calls = []                   # [(eid, success, credit, detail)]

    async def match(self, context, domain=None, max_results=8, **kw):
        if self._match_error:
            raise RuntimeError("qdrant down")
        return self._matches

    async def hebbian_update(self, eid, success, expected=None, detail="", credit=1.0):
        self.hebbian_calls.append((eid, success, credit, detail))
        return {"enzyme_id": eid}


def _fake_llm(verdicts):
    """替换 replay.chat_json:断言只收到考卷,返回预置判决。"""
    async def fake(system="", user=""):
        return verdicts
    return fake


# ── 用例 ───────────────────────────────────────────────────────

def test_exam_excludes_answers():
    ep = _episode()
    exam = replay.exam_context(ep)
    for known in ("修复 JWT 刷新令牌死循环", "自动续期", "拦截器重复注册",
                  "auth.ts", "401 Unauthorized loop"):
        assert known in exam, f"考题应含事发信息: {known}"
    for answer in ("热重载后重复注册", "去重注册后解决", "注册要幂等", "console: 401"):
        assert answer not in exam, f"考题泄露答案: {answer}"
    print("[ok] exam_excludes_answers")


def test_self_sourced_guard():
    ep = _episode(enzyme_ids=["e1"])
    assert replay.is_self_sourced(_enzyme("e1"), ep)                    # 提炼关联
    ref = f"episode:{ep.id}|2026-07-02|修复 JWT"
    assert replay.is_self_sourced(_enzyme("e9", references=[ref]), ep)  # 出处指针
    assert not replay.is_self_sourced(_enzyme("e2"), ep)
    print("[ok] self_sourced_guard")


async def test_scoring_and_records():
    ep = _episode(enzyme_ids=["e0"])
    self_ref = f"episode:{ep.id}|2026-07-02|t"
    eng = FakeEngine(matches=[
        _R(_enzyme("e0")),                          # 提炼来源 → 排除
        _R(_enzyme("e5", references=[self_ref])),   # 出处指针 → 排除
        _R(_enzyme("e1")), _R(_enzyme("e2")), _R(_enzyme("e3")),
    ])
    replay.chat_json = _fake_llm([
        {"id": "e1", "verdict": "helped", "evidence": "与教训一致"},
        {"id": "e2", "verdict": "contradicted", "evidence": "与根因相悖"},
        {"id": "e3", "verdict": "neutral", "evidence": ""},
        {"id": "e0", "verdict": "helped", "evidence": "自证"},     # 未进考场 → 忽略
        {"id": "ghost", "verdict": "helped", "evidence": ""},      # 未知 id → 忽略
        "garbage",                                                  # 非 dict → 忽略
    ])
    summary = {"helped": 0, "contradicted": 0, "neutral": 0,
               "self_excluded": 0, "skipped_empty": 0, "errors": 0}
    await replay.replay_episode(eng, ep, summary)

    assert summary["self_excluded"] == 2, summary
    assert (summary["helped"], summary["contradicted"], summary["neutral"]) == (1, 1, 1)
    assert eng.hebbian_calls == [
        ("e1", True, 0.5, "replay: 与教训一致"),
        ("e2", False, 0.5, "replay: 与根因相悖"),
    ], eng.hebbian_calls                            # neutral 不计分,credit 减半
    assert [(r[1], r[2]) for r in eng.pg.recorded] == [
        ("e1", "helped"), ("e2", "contradicted"), ("e3", "neutral")]
    assert all(ev.event_type == EventType.REPLAY for ev in eng.pg.events)
    assert eng.pg.events[0].detail["episode_id"] == ep.id
    print("[ok] scoring_and_records")


async def test_idempotent_terminal():
    """判过的 (幕,酶) 不重判——neutral 也是终局(情景冻结,与 capture 不同)。"""
    ep = _episode()
    eng = FakeEngine(matches=[_R(_enzyme("e1")), _R(_enzyme("e2"))],
                     replayed={(ep.id, "e1")})
    replay.chat_json = _fake_llm([{"id": "e2", "verdict": "helped", "evidence": "x"}])
    summary = {"helped": 0, "contradicted": 0, "neutral": 0,
               "self_excluded": 0, "skipped_empty": 0, "errors": 0}
    await replay.replay_episode(eng, ep, summary)
    assert [c[0] for c in eng.hebbian_calls] == ["e2"]
    assert all(r[1] != "e1" for r in eng.pg.recorded)
    print("[ok] idempotent_terminal")


async def test_llm_garbage_and_invalid_verdict():
    ep = _episode()
    eng = FakeEngine(matches=[_R(_enzyme("e1"))])
    replay.chat_json = _fake_llm("not a list")     # 整体非法 → 空手而归
    summary = {"helped": 0, "contradicted": 0, "neutral": 0,
               "self_excluded": 0, "skipped_empty": 0, "errors": 0}
    await replay.replay_episode(eng, ep, summary)
    assert not eng.hebbian_calls and not eng.pg.recorded

    eng2 = FakeEngine(matches=[_R(_enzyme("e1"))])
    replay.chat_json = _fake_llm([{"id": "e1", "verdict": "AMAZING", "evidence": "x"}])
    await replay.replay_episode(eng2, ep, summary)
    assert not eng2.hebbian_calls                  # 未知判决降级 neutral,不计分
    assert eng2.pg.recorded[0][2] == "neutral"
    print("[ok] llm_garbage_and_invalid_verdict")


async def test_empty_exam_skipped():
    ep = _episode(title=" ", intent="", events=[], entities=[],
                  error_signatures=[])
    eng = FakeEngine(matches=[_R(_enzyme("e1"))])
    summary = {"helped": 0, "contradicted": 0, "neutral": 0,
               "self_excluded": 0, "skipped_empty": 0, "errors": 0}
    await replay.replay_episode(eng, ep, summary)
    assert summary["skipped_empty"] == 1 and not eng.hebbian_calls
    print("[ok] empty_exam_skipped")


async def test_cycle_isolation():
    """一幕考试失败不影响其余幕。"""
    ep1, ep2 = _episode("ep1"), _episode("ep2")
    eng = FakeEngine(matches=[_R(_enzyme("e1"))], episodes=[ep1, ep2])
    calls = {"n": 0}

    async def flaky_match(context, domain=None, max_results=8, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("qdrant hiccup")
        return [_R(_enzyme("e1"))]

    eng.match = flaky_match
    replay.chat_json = _fake_llm([{"id": "e1", "verdict": "helped", "evidence": "x"}])
    summary = await replay.run_replay_cycle(eng)
    assert summary["episodes"] == 2 and summary["errors"] == 1
    assert summary["helped"] == 1                  # 第二幕照常判卷
    print("[ok] cycle_isolation")


async def main():
    print("=" * 50)
    print("情景回放测试单测")
    print("=" * 50)
    test_exam_excludes_answers()
    test_self_sourced_guard()
    await test_scoring_and_records()
    await test_idempotent_terminal()
    await test_llm_garbage_and_invalid_verdict()
    await test_empty_exam_skipped()
    await test_cycle_isolation()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    asyncio.run(main())
