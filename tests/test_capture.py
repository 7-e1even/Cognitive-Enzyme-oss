"""Capture 写路径单测 — 纯 mock,无需起服务/连库。

直接跑: python tests/test_capture.py
覆盖:候选解析、salience 过滤、确定性 ADD(无命中)、对账路由
     (ADD / UPDATE / REINFORCE / INVALIDATE / SKIP)。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.learning import capture  # noqa: E402


# ── Fakes ──────────────────────────────────────────────────────

class _Emb:
    pass


class FakeEngine:
    """记录被调用的引擎操作,供断言。"""

    def __init__(self, hits=None, enzymes=None, harvest_status="active"):
        self.config = type("C", (), {"embedding": _Emb()})()
        self._hits = hits or []
        self._enzymes = enzymes or {}
        self._harvest_status = harvest_status
        self.calls = []
        self.qdrant = type("Q", (), {"search": self._search})()
        self.pg = type("P", (), {"log_event": self._log_event})()

    async def _search(self, vector, limit=5, include_all=False):
        return self._hits

    async def _log_event(self, event):
        self.calls.append(("log_event", event.event_type))

    async def harvest(self, insight, context="", domain="", tags=None):
        self.calls.append(("harvest", insight))
        return {"status": self._harvest_status}

    async def get_enzyme(self, eid):
        self.calls.append(("get_enzyme", eid))
        return self._enzymes.get(eid)

    async def update_enzyme(self, enzyme):
        self.calls.append(("update_enzyme", enzyme.id))

    async def hebbian_update(self, enzyme_id, success, expected=None, detail=""):
        self.calls.append(("hebbian", enzyme_id, success))
        return {"enzyme_id": enzyme_id}


def _actions(engine):
    return [c[0] for c in engine.calls]


def _patch_llm(monkey_extract, monkey_reconcile, monkey_embed=True):
    """打桩 capture 模块里的 chat_json / embed。"""
    async def fake_chat_json(system, user, config=None):
        if "JSON 数组" in user:       # extract 提示
            return monkey_extract
        return monkey_reconcile        # reconcile 提示(JSON 对象)

    async def fake_embed(text, config=None):
        return [0.1, 0.2, 0.3, 0.4] if monkey_embed else None

    capture.chat_json = fake_chat_json
    capture.embed = fake_embed


# ── 测试用例 ────────────────────────────────────────────────────

async def test_extract_parsing():
    _patch_llm([
        {"trigger": "用 JWT 鉴权", "then": "刷新令牌走 httpOnly cookie",
         "because": "防 XSS 窃取", "salience": 0.9, "tags": ["auth"]},
        {"then": "", "salience": 0.9},   # 空 then 应被丢弃
    ], {})
    cands = await capture.extract_candidates("some transcript", domain="sec")
    assert len(cands) == 1, cands
    assert cands[0]["then"].startswith("刷新令牌")
    assert cands[0]["domain"] == "sec"
    print("[ok] extract_parsing")


async def test_no_hits_adds():
    """无相似酶 → 走确定性 ADD(harvest),不调 reconcile。"""
    eng = FakeEngine(hits=[])
    _patch_llm([{"then": "全新经验", "salience": 0.9}], {"action": "SKIP"})
    summary = await capture.capture_session(eng, "t")
    assert summary["added"] == 1, summary
    assert "harvest" in _actions(eng)
    print("[ok] no_hits_adds")


async def test_salience_filter():
    eng = FakeEngine(hits=[])
    _patch_llm([{"then": "鸡毛蒜皮", "salience": 0.2}], {})
    summary = await capture.capture_session(eng, "t")
    assert summary["skipped"] == 1 and summary["added"] == 0, summary
    assert "harvest" not in _actions(eng)
    print("[ok] salience_filter")


async def test_reconcile_update():
    from app.core import Enzyme, Pattern
    target = Enzyme(id="e1", patterns=[Pattern(value="jwt")], then="旧建议", confidence=0.5)
    eng = FakeEngine(
        hits=[{"id": "e1", "then": "旧建议", "because": "", "_score": 0.8}],
        enzymes={"e1": target},
    )
    _patch_llm([{"then": "更完整的建议", "salience": 0.9}],
               {"action": "UPDATE", "target_id": "e1",
                "then": "更完整的建议", "because": "补充理由"})
    summary = await capture.capture_session(eng, "t")
    assert summary["updated"] == 1, summary
    assert target.then == "更完整的建议" and target.because == "补充理由"
    assert target.confidence > 0.5  # 增补会轻微提升置信
    assert "update_enzyme" in _actions(eng)
    print("[ok] reconcile_update")


async def test_reconcile_reinforce():
    eng = FakeEngine(hits=[{"id": "e1", "then": "x", "because": "", "_score": 0.9}])
    _patch_llm([{"then": "再次印证", "salience": 0.9}],
               {"action": "REINFORCE", "target_id": "e1"})
    summary = await capture.capture_session(eng, "t")
    assert summary["reinforced"] == 1, summary
    assert ("hebbian", "e1", True) in eng.calls
    print("[ok] reconcile_reinforce")


async def test_reconcile_invalidate():
    eng = FakeEngine(hits=[{"id": "e1", "then": "过时建议", "because": "", "_score": 0.8}])
    _patch_llm([{"then": "推翻旧的", "salience": 0.9}],
               {"action": "INVALIDATE", "target_id": "e1", "then": "推翻旧的"})
    summary = await capture.capture_session(eng, "t")
    assert summary["invalidated"] == 1, summary
    # 纠错:hebbian(False) + 新增 harvest
    assert ("hebbian", "e1", False) in eng.calls
    assert "harvest" in _actions(eng)
    print("[ok] reconcile_invalidate")


async def test_reconcile_skip():
    eng = FakeEngine(hits=[{"id": "e1", "then": "已有", "because": "", "_score": 0.9}])
    _patch_llm([{"then": "纯重复", "salience": 0.9}],
               {"action": "SKIP", "target_id": "e1"})
    summary = await capture.capture_session(eng, "t")
    assert summary["skipped"] == 1 and summary["added"] == 0, summary
    print("[ok] reconcile_skip")


_KNOWN_CAND = [{"trigger": "遇到复杂问题没有思路", "then": "先解一个更简单的同类问题", "salience": 0.9}]


async def test_known_no_hits_counted():
    """无命中走确定性 ADD,harvest 回了 known(模型已会)→ 计 known 不计 added。"""
    eng = FakeEngine(hits=[], harvest_status="known")
    _patch_llm(_KNOWN_CAND, {})
    summary = await capture.capture_session(eng, "t")
    assert summary["known"] == 1 and summary["added"] == 0, summary
    assert "harvest" in _actions(eng)          # 已会过滤收在 harvest 里,仍走 harvest
    print("[ok] known_no_hits_counted")


async def test_known_reconcile_add_counted():
    """对账判 ADD,harvest 回 known → 计 known 不计 added。"""
    eng = FakeEngine(hits=[{"id": "e1", "then": "x", "because": "", "_score": 0.9}],
                     harvest_status="known")
    _patch_llm(_KNOWN_CAND, {"action": "ADD", "then": "先解一个更简单的同类问题"})
    summary = await capture.capture_session(eng, "t")
    assert summary["known"] == 1 and summary["added"] == 0, summary
    print("[ok] known_reconcile_add_counted")


async def test_count_new_summary_without_known_key():
    """外部调用方传的 summary 没有 "known" 键 → 照样计数,不 KeyError。"""
    eng = FakeEngine(harvest_status="known")
    summary = {"added": 0, "updated": 0, "reinforced": 0, "invalidated": 0, "skipped": 0, "errors": 0}
    await capture._apply(eng, _cand(), {"action": "ADD", "then": "x"}, summary)
    assert summary.get("known") == 1 and summary["added"] == 0, summary
    print("[ok] count_new summary without known key")


# ── UPDATE 正文归并:补丁式回复 / 超长封顶 ─────────────────────────

async def test_merge_then_patch_notes():
    """LLM 只回"说明+补充"时:补充段追加到旧正文后面,原文不丢。"""
    old = "旧正文A"
    for open_p, close_p in (("（", "）"), ("(", ")")):
        for word in ("保留", "不变", "增补", "追加", "补充"):
            new = f"{open_p}原then全部{word}{close_p}新增B"
            final, mode = capture._merge_then(old, new)
            assert (final, mode) == ("旧正文A\n\n新增B", "append"), (word, final, mode)
    # 全角括注里夹半角括号(真实样本):收尾必须是同款全角括号,不能在 (i) 的半角 ) 处截断
    final, mode = capture._merge_then(old, "（原then全部内容保留不变:六步清单(i)–(vi)、力预检维度）新增C")
    assert (final, mode) == ("旧正文A\n\n新增C", "append"), (final, mode)
    print("[ok] merge_then patch_notes")


async def test_merge_then_noop_and_replace():
    """只有说明没有内容 → noop;正常完整正文与无关括注 → replace。"""
    assert capture._merge_then("旧A", "（原then保留不变）") == ("旧A", "noop")
    assert capture._merge_then("旧A", "完整新正文B") == ("完整新正文B", "replace")
    assert capture._merge_then("旧A", "（物理）先画图再列方程") == ("（物理）先画图再列方程", "replace")
    print("[ok] merge_then noop/replace")


def _cand(then="候选正文全文", **kw):
    return {"then": then, "because": "", "trigger": "", "domain": "", "tags": [], **kw}


def _summary():
    return {"extracted": 0, "added": 0, "updated": 0, "reinforced": 0,
            "invalidated": 0, "skipped": 0, "known": 0, "errors": 0,
            "passive_helped": 0, "passive_contradicted": 0, "passive_neutral": 0}


class _EvtEngine(FakeEngine):
    """把 log_event 的 detail 一并记下。"""

    async def _log_event(self, event):
        self.calls.append(("log_event", event.event_type, event.detail))


async def test_update_patch_note_appends():
    """UPDATE 回了补丁式 then → 追加而非覆盖;事件 detail 记 merge_mode/prev_then。"""
    from app.core import Enzyme, Pattern
    target = Enzyme(id="e1", patterns=[Pattern(value="jwt")], then="旧建议", because="旧理由")
    eng = _EvtEngine(enzymes={"e1": target})
    summary = _summary()
    await capture._apply(eng, _cand(),
                         {"action": "UPDATE", "target_id": "e1",
                          "then": "（原then全部保留不变）新增镜像减半通道", "reason": "补充"},
                         summary)
    assert target.then == "旧建议\n\n新增镜像减半通道"
    assert target.because == "旧理由"
    detail = next(c[2] for c in eng.calls if c[0] == "log_event")
    assert detail["merge_mode"] == "append" and detail["prev_then"] == "旧建议"
    assert summary["updated"] == 1
    print("[ok] update_patch_note")


async def test_update_overflow_adds_new():
    """增补后超 learning.update_max_chars 且变长 → 旧记忆不动,候选按自身正文另存新增。"""
    from app import settings
    from app.core import Enzyme, Pattern
    target = Enzyme(id="e1", patterns=[Pattern(value="jwt")], then="旧建议")
    eng = FakeEngine(enzymes={"e1": target})
    summary = _summary()
    with settings.override({"learning.update_max_chars": 5}):
        await capture._apply(eng, _cand(),
                             {"action": "UPDATE", "target_id": "e1",
                              "then": "旧建议 再加一大段新内容"}, summary)
    assert target.then == "旧建议"                     # 目标原样
    assert ("harvest", "候选正文全文") in eng.calls       # 候选按自己的正文走新增
    assert "update_enzyme" not in _actions(eng)
    assert summary["added"] == 1 and summary["updated"] == 0
    print("[ok] update_overflow_adds_new")


async def test_update_overflow_off_and_shrink():
    """cap=0 不封顶;结果虽超 cap 但比原文短(收紧)→ 照样替换。"""
    from app import settings
    from app.core import Enzyme, Pattern
    target = Enzyme(id="e1", patterns=[Pattern(value="p")], then="旧建议")
    eng = FakeEngine(enzymes={"e1": target})
    summary = _summary()
    with settings.override({"learning.update_max_chars": 0}):
        await capture._apply(eng, _cand(), {"action": "UPDATE", "target_id": "e1",
                                           "then": "x" * 5000}, summary)
    assert target.then == "x" * 5000                    # 0 = 不限

    target2 = Enzyme(id="e2", patterns=[Pattern(value="p")], then="长" * 2000)
    eng2 = FakeEngine(enzymes={"e2": target2})
    with settings.override({"learning.update_max_chars": 1200}):
        await capture._apply(eng2, _cand(), {"action": "UPDATE", "target_id": "e2",
                                            "then": "短" * 1500}, summary)
    assert target2.then == "短" * 1500                  # 1500 > 1200 但变短 → 允许
    print("[ok] update_overflow_off_and_shrink")


async def main():
    print("=" * 50)
    print("Capture 写路径单测")
    print("=" * 50)
    for t in [test_extract_parsing, test_no_hits_adds, test_salience_filter,
              test_reconcile_update, test_reconcile_reinforce,
              test_reconcile_invalidate, test_reconcile_skip,
              test_known_no_hits_counted, test_known_reconcile_add_counted,
              test_count_new_summary_without_known_key,
              test_merge_then_patch_notes, test_merge_then_noop_and_replace,
              test_update_patch_note_appends, test_update_overflow_adds_new,
              test_update_overflow_off_and_shrink]:
        await t()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    asyncio.run(main())
