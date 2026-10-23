"""情景层(episodes)单测 — 纯 mock,无需起服务/连库。

直接跑: python tests/test_episodes.py
覆盖:线索归一化、情景卡提取(含 null/低 salience 拒收)、capture 集成
     (写情景卡 + 关联触达酶 + 出处回填)、formatter 出处剥离与骨架/详情渲染。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.learning import capture, episodes  # noqa: E402


# ── Fakes ──────────────────────────────────────────────────────

class _Emb:
    pass


class FakeEngine:
    """记录被调用的引擎/存储操作,供断言。"""

    def __init__(self, hits=None):
        self.config = type("C", (), {"embedding": _Emb()})()
        self._hits = hits or []
        self.calls = []
        self.episodes = []
        self.refs = []
        self.qdrant = type("Q", (), {"search": self._search})()
        self.pg = type("P", (), {
            "log_event": self._log_event,
            "upsert_episode": self._upsert_episode,
            "set_episode_ref": self._set_episode_ref,
        })()

    async def _search(self, vector, limit=5, include_all=False):
        return self._hits

    async def _log_event(self, event):
        self.calls.append(("log_event", event.event_type))

    async def _upsert_episode(self, episode):
        self.episodes.append(episode)

    async def _set_episode_ref(self, enzyme_id, ref):
        self.refs.append((enzyme_id, ref))

    async def harvest(self, insight, context="", domain="", tags=None):
        self.calls.append(("harvest", insight))
        return {"id": f"harvested-{len(self.calls)}", "status": "active"}

    async def get_enzyme(self, eid):
        return None

    async def hebbian_update(self, enzyme_id, success, expected=None, detail=""):
        self.calls.append(("hebbian", enzyme_id, success))
        return {"enzyme_id": enzyme_id}


_EPISODE_CARD = {
    "title": "修复 JWT 刷新死循环",
    "intent": "登录后 token 自动续期",
    "events": ["复现 401 循环", "定位到 JwtRefresher 重入", "改为单飞锁后通过"],
    "root_cause": "刷新请求本身带过期 token,又触发刷新",
    "outcome": "解决了,回归通过",
    "lesson": "刷新端点必须豁免鉴权中间件",
    "entities": ["JwtRefresher", "auth/refresh.py"],
    "error_signatures": ["HTTP 401 Unauthorized (loop x32)"],
    "excerpt": "user: 登录之后一直 401\nassistant: JwtRefresher 在刷新时又带上了过期 token…",
    "salience": 0.9,
}


def _patch_llm(extract_cands, episode_card, reconcile=None):
    """打桩 capture / episodes 两个模块的 chat_json 与 embed。"""
    async def fake_capture_chat(system, user, config=None):
        if "JSON 数组" in user:
            return extract_cands
        return reconcile or {"action": "SKIP"}

    async def fake_episode_chat(system, user, config=None):
        return episode_card

    async def fake_embed(text, config=None):
        return [0.1, 0.2, 0.3]

    capture.chat_json = fake_capture_chat
    capture.embed = fake_embed
    episodes.chat_json = fake_episode_chat


# ── 线索归一化 ─────────────────────────────────────────────────

def test_cues_from_text():
    cues = episodes.cues_from_text("修复 JwtRefresher 死循环 token.refresh HTTP 401")
    assert "jwtrefresher" in cues and "token.refresh" in cues, cues
    assert "efresher" not in cues          # 词边界:不从标识符内部咬词
    assert cues == list(dict.fromkeys(cues))  # 去重且保序
    print("[ok] cues_from_text")


def test_build_cues():
    cues = episodes.build_cues(_EPISODE_CARD)
    assert "jwtrefresher" in cues, cues
    assert "重入" in cues, cues       # 事件链也参与场景回想
    assert "回归通过" in cues, cues  # 结局也参与场景回想
    assert "recursionerror" not in cues
    assert any("401" in c or "unauthorized" in c for c in cues), cues
    assert len(cues) <= 24
    print("[ok] build_cues")


# ── 情景卡提取 ─────────────────────────────────────────────────

async def test_extract_episode():
    _patch_llm([], _EPISODE_CARD)
    card = await episodes.extract_episode("some transcript", domain="auth")
    assert card and card["title"].startswith("修复 JWT"), card
    assert card["domain"] == "auth"
    assert len(card["events"]) == 3
    print("[ok] extract_episode")


async def test_extract_episode_null():
    _patch_llm([], None)
    assert await episodes.extract_episode("闲聊") is None
    _patch_llm([], {**_EPISODE_CARD, "salience": 0.1})   # 琐碎一幕拒收
    assert await episodes.extract_episode("t") is None
    _patch_llm([], {**_EPISODE_CARD, "title": ""})       # 无题拒收
    assert await episodes.extract_episode("t") is None
    print("[ok] extract_episode_null")


def test_card_to_episode():
    ep = episodes.card_to_episode(_EPISODE_CARD, session_id="s1",
                                  enzyme_ids=["e1", "e1", "e2"])
    assert ep.enzyme_ids == ["e1", "e2"]   # 去重
    assert ep.cues and ep.title == _EPISODE_CARD["title"]
    assert ep.session_id == "s1"
    print("[ok] card_to_episode")


# ── capture 集成 ───────────────────────────────────────────────

async def test_capture_writes_episode():
    """ADD 路径:酶入库后,情景卡应带上该酶 id,并回填出处指针。"""
    eng = FakeEngine(hits=[])
    _patch_llm([{"then": "刷新端点豁免鉴权", "salience": 0.9}], _EPISODE_CARD)
    summary = await capture.capture_session(eng, "t", session_id="s1")
    assert summary["added"] == 1 and "episode" in summary, summary
    assert len(eng.episodes) == 1
    ep = eng.episodes[0]
    assert ep.session_id == "s1" and ep.enzyme_ids, ep
    assert eng.refs and eng.refs[0][0] == ep.enzyme_ids[0]
    assert eng.refs[0][1].startswith(f"episode:{ep.id}|")
    print("[ok] capture_writes_episode")


async def test_capture_reinforce_links_target():
    """REINFORCE 路径:被强化的旧酶应成为情景卡的关联对象。"""
    eng = FakeEngine(hits=[{"id": "e1", "then": "x", "because": "", "_score": 0.9}])
    _patch_llm([{"then": "再次印证", "salience": 0.9}], _EPISODE_CARD,
               reconcile={"action": "REINFORCE", "target_id": "e1"})
    summary = await capture.capture_session(eng, "t")
    assert summary["reinforced"] == 1, summary
    assert eng.episodes and eng.episodes[0].enzyme_ids == ["e1"]
    print("[ok] capture_reinforce_links_target")


async def test_capture_episode_failure_nonfatal():
    """情景写入炸了不影响主流程(best-effort)。"""
    eng = FakeEngine(hits=[])

    async def boom(episode):
        raise RuntimeError("pg down")
    eng.pg.upsert_episode = boom

    _patch_llm([{"then": "经验", "salience": 0.9}], _EPISODE_CARD)
    summary = await capture.capture_session(eng, "t")
    assert summary["added"] == 1 and "episode" not in summary, summary
    print("[ok] capture_episode_failure_nonfatal")


async def test_capture_trivial_no_episode():
    """琐碎会话:提不出情景卡就不写。"""
    eng = FakeEngine(hits=[])
    _patch_llm([], None)
    summary = await capture.capture_session(eng, "闲聊两句")
    assert "episode" not in summary and not eng.episodes, summary
    print("[ok] capture_trivial_no_episode")


# ── formatter ──────────────────────────────────────────────────

def test_formatter_provenance():
    from app.delivery.formatter import (
        _split_provenance, format_episode_detail, format_episode_skeleton,
    )
    prov, rest = _split_provenance(
        ["https://example.com/doc", "episode:abc|2026-07-02|修复 JWT 刷新死循环"])
    assert prov == "2026-07-02 修复 JWT 刷新死循环 (episode:abc)", prov
    assert rest == ["https://example.com/doc"]
    prov2, rest2 = _split_provenance(["plain-ref"])
    assert prov2 is None and rest2 == ["plain-ref"]

    ep = episodes.card_to_episode(_EPISODE_CARD, enzyme_ids=["e1"])
    skel = format_episode_skeleton(ep, overlap=3)
    assert skel["overlap"] == 3 and "excerpt" not in skel    # 骨架不带细节
    detail = format_episode_detail(ep)
    assert detail["excerpt"] and detail["events"], detail
    print("[ok] formatter_provenance")


async def main():
    print("=" * 50)
    print("情景层(episodes)单测")
    print("=" * 50)
    test_cues_from_text()
    test_build_cues()
    test_card_to_episode()
    for t in [test_extract_episode, test_extract_episode_null,
              test_capture_writes_episode, test_capture_reinforce_links_target,
              test_capture_episode_failure_nonfatal, test_capture_trivial_no_episode]:
        await t()
    test_formatter_provenance()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    asyncio.run(main())
