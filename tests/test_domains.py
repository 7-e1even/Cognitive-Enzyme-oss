"""领域自动归类:判官归入已有领域,都不合适时语言模型新建;上限、兜底与失败都退回原行为。"""

import pytest

from app.config import TypeSafeConfig
from app.learning import domains

TS = TypeSafeConfig(api_key="k", api_url="http://ts")


class FakePG:
    def __init__(self, rows):
        self.domains = {r["name"]: {"description": "", "source": "existing", "size": 1, **r} for r in rows}

    async def list_domains(self):
        return [dict(d) for d in self.domains.values()]

    async def add_domain(self, name, description, source):
        if name in self.domains:
            return False
        self.domains[name] = {"name": name, "description": description, "source": source, "size": 0}
        return True

    async def describe_domain(self, name, description):
        if not self.domains[name]["description"]:
            self.domains[name]["description"] = description

    async def domain_samples(self, name, limit=5):
        return [f"{name} 的一条记忆"]


def _library():
    return FakePG([{"name": "legal", "description": "判例与法律论证"},
                   {"name": "biomed", "description": "生物医学论文结论"}])


@pytest.fixture
def models(monkeypatch):
    """假判官(按 probs 作答)与假语言模型(按 proposal / description 作答),记下调用。"""
    state = {"probs": None, "proposal": None, "description": "自动写的描述", "judge": 0, "llm": 0, "options": None}

    async def fake_system_one(s, questions, config, timeout=10.0):
        state["judge"] += 1
        state["options"] = questions["domain"]["criteria"]
        return None if state["probs"] is None else {"answers": {"domain": {"probabilities": state["probs"]}}}

    async def fake_chat_json(system, user, config=None, timeout=30.0):
        state["llm"] += 1
        if "说明这个领域收录什么" in user:
            return {"description": state["description"]}
        return state["proposal"]

    monkeypatch.setattr(domains, "system_one", fake_system_one)
    monkeypatch.setattr(domains, "chat_json", fake_chat_json)
    return state


async def _resolve(pg, hint=""):
    return await domains.resolve_domain(pg, "先查判例再下结论", "用户问合同违约怎么判", hint, TS)


@pytest.mark.asyncio
async def test_judge_puts_memory_into_existing_domain(models):
    models["probs"] = {"d0": 0.8, "d1": 0.1, "none": 0.1}
    name, note = await _resolve(_library())
    assert name == "legal" and note["via"] == "judge" and models["llm"] == 0
    assert models["options"]["d0"] == "legal:判例与法律论证"   # 选项带描述


@pytest.mark.asyncio
async def test_new_domain_created_when_nothing_fits(models):
    pg = _library()
    models["probs"] = {"d0": 0.2, "d1": 0.1, "none": 0.7}
    models["proposal"] = {"name": "前端工程", "description": "网页前端开发的经验"}
    name, note = await _resolve(pg)
    assert name == "前端工程" and note["via"] == "created"
    assert pg.domains["前端工程"]["source"] == "created"


@pytest.mark.asyncio
async def test_below_threshold_is_not_assigned(models):
    models["probs"] = {"d0": 0.45, "d1": 0.2, "none": 0.35}      # 最高项没到 0.5
    models["proposal"] = {"name": "LEGAL", "description": ""}      # 模型认定属于已有领域(大小写不同)
    pg = _library()
    name, note = await _resolve(pg)
    assert name == "legal" and note["via"] == "llm" and set(pg.domains) == {"legal", "biomed"}


@pytest.mark.asyncio
async def test_given_known_domain_is_kept_without_calls(models):
    name, note = await _resolve(_library(), hint="biomed")
    assert name == "biomed" and note["via"] == "given" and models["judge"] == models["llm"] == 0


@pytest.mark.asyncio
async def test_cap_reached_uses_closest_domain(models, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_MAX", "2")
    models["probs"] = {"d0": 0.3, "d1": 0.1, "none": 0.6}
    pg = _library()
    name, note = await _resolve(pg)
    assert name == "legal" and note["via"] == "closest" and models["llm"] == 0 and len(pg.domains) == 2


@pytest.mark.asyncio
async def test_without_judge_the_llm_picks_or_creates(models):
    models["proposal"] = {"name": "biomed", "description": ""}
    name, note = await _resolve(_library())
    assert name == "biomed" and note["via"] == "llm" and models["judge"] == 1


@pytest.mark.asyncio
async def test_failures_keep_the_given_domain(models):
    name, note = await _resolve(_library(), hint="工程工作流")
    assert name == "工程工作流" and note["via"] == "failed"


@pytest.mark.asyncio
async def test_switched_off_keeps_the_given_domain(models, monkeypatch):
    monkeypatch.setenv("CE_DOMAIN_AUTO", "off")
    name, note = await _resolve(_library(), hint="随手起的名字")
    assert name == "随手起的名字" and note["via"] == "off" and models["judge"] == models["llm"] == 0


@pytest.mark.asyncio
async def test_first_memory_creates_the_first_domain(models):
    pg = FakePG([])
    models["proposal"] = {"name": "法律", "description": "法律问题与判例"}
    name, note = await _resolve(pg)
    assert name == "法律" and note["via"] == "created" and models["judge"] == 0


@pytest.mark.asyncio
async def test_missing_descriptions_are_written_once(models):
    pg = FakePG([{"name": "firefox"}, {"name": "legal", "description": "判例"}])
    models["probs"] = {"d0": 0.9, "d1": 0.05, "none": 0.05}
    await _resolve(pg)
    assert pg.domains["firefox"]["description"] == "自动写的描述" and models["llm"] == 1
    await _resolve(pg)
    assert models["llm"] == 1                      # 已有描述,不再重写


# ── 巩固时的小领域归并 ──────────────────────────────────────────────

class MergePG(FakePG):
    """带记忆的假库:领域条数由记忆实时算出。"""

    def __init__(self, rows, enzymes):
        super().__init__(rows)
        self.enzymes = {e.id: e for e in enzymes}
        self.events = []

    async def list_domains(self):
        sizes = {}
        for e in self.enzymes.values():
            sizes[e.domain] = sizes.get(e.domain, 0) + 1
        return [dict(d, size=sizes.get(d["name"], 0)) for d in self.domains.values()]

    async def list_enzymes(self, domain=None, min_confidence=0.0, limit=50):
        return [e for e in self.enzymes.values() if e.domain == domain][:limit]

    async def upsert_enzyme(self, e, expected_version=None):
        self.enzymes[e.id] = e
        return 1

    async def log_event(self, event):
        self.events.append(event)

    async def delete_domain(self, name):
        if any(e.domain == name for e in self.enzymes.values()):
            return False
        return self.domains.pop(name, None) is not None


class FakeSync:
    def __init__(self):
        self.synced = []

    async def sync_enzyme(self, e, generation=None):
        self.synced.append(e.id)


def _merge_library():
    from app.core import Enzyme, Pattern
    mk = lambda eid, dom: Enzyme(id=eid, patterns=[Pattern(value=eid)], then=f"then {eid}", domain=dom)  # noqa: E731
    enzymes = [mk(f"l{i}", "legal") for i in range(3)] + [mk("x1", "法律检索与论证"), mk("m1", "数学与概率")]
    rows = [{"name": "legal", "description": "判例"}, {"name": "法律检索与论证", "description": "法律检索"},
            {"name": "数学与概率", "description": "数学", "source": "created"}]
    return MergePG(rows, enzymes)


@pytest.mark.asyncio
async def test_small_domain_members_move_into_larger_domain(models, monkeypatch):
    pg, sync = _merge_library(), FakeSync()
    answers = {"x1": {"d0": 0.9, "none": 0.1}, "m1": {"d0": 0.05, "none": 0.95}}

    async def per_memory(s, questions, config, timeout=10.0):
        assert list(questions["domain"]["criteria"]) == ["d0", "none"]    # 只往成规模的 legal 并
        eid = s["memory"]["情境"]
        return {"answers": {"domain": {"probabilities": answers[eid]}}}

    monkeypatch.setattr(domains, "system_one", per_memory)
    moved = await domains.merge_small_domains(pg, sync, TS)
    assert moved == 1 and pg.enzymes["x1"].domain == "legal" and sync.synced == ["x1"]
    assert "法律检索与论证" not in pg.domains            # 搬空的领域删掉
    assert pg.enzymes["m1"].domain == "数学与概率" and "数学与概率" in pg.domains   # 判官说都不合适:留着
    assert pg.events[0].detail["to"] == "legal"


@pytest.mark.asyncio
async def test_merge_does_nothing_without_judge_or_when_off(models, monkeypatch):
    pg, sync = _merge_library(), FakeSync()
    assert await domains.merge_small_domains(pg, sync, TS) == 0          # 判官不可用(返回 None)
    assert pg.enzymes["x1"].domain == "法律检索与论证" and "法律检索与论证" in pg.domains
    monkeypatch.setenv("CE_DOMAIN_MERGE_BELOW", "0")
    models["probs"], before = {"d0": 0.9, "none": 0.1}, models["judge"]
    assert await domains.merge_small_domains(pg, sync, TS) == 0 and models["judge"] == before   # 关闭:一次都不问
