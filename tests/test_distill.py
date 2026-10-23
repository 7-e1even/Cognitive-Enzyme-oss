"""判定记录平台单测 — 老师注册表 / 提示渲染 / 答案解析(纯内存,不联网)。"""
import asyncio
import json

import pytest

from app.distill import teachers as T

RULE = {"触发": "升级到 139", "建议": "侧边栏状态丢失时先清掉 xulstore.json 再重启,这是已知回归", "依据": "bug 1940533"}
QUESTIONS = {
    "c0": {"type": "noul", "instructions": {"rule": RULE, "question": "`rule` 的建议是否直接有用?"},
           "criteria": {"true": "正是该场景", "false": "无关"}},
    "c1": {"type": "noul", "instructions": {"rule": {"建议": "短"}, "question": "`rule` 的建议是否直接有用?"},
           "criteria": {"true": "正是该场景", "false": "无关"}},
    "best": {"type": "choice", "instructions": "哪一条最有用?",
             "criteria": {"c0": RULE, "c1": {"建议": "短"}, "none": "都没用"}},
}


def test_render_dedups_long_materials():
    text = T.render_prompt("用户:升级后侧边栏折叠状态没了", QUESTIONS)
    assert text.count("xulstore.json") == 1                 # 同一条规则只出现一次,两处都引用 [M0]
    assert text.count("[M0]") == 3 and "[c0] (noul)" in text and "[best] (choice)" in text
    assert '{"建议": "短"}' in text                           # 短素材直接内联


def test_parse_answers_normalizes_and_tolerates_noise():
    out = T.parse_answers('思考完毕。\n{"c0": 0.9, "c1": 1.7, "best": {"c0": 3, "c1": 1, "none": 0},}', QUESTIONS)
    assert out["c0"] == {"type": "noul", "noul": 0.9}
    assert out["c1"]["noul"] == 1.0                           # 夹到 [0, 1]
    assert out["best"]["probabilities"] == {"c0": 0.75, "c1": 0.25, "none": 0.0}
    assert out["best"]["choice"] == "c0"
    assert T.parse_answers("没有 JSON", QUESTIONS) is None
    partial = T.parse_answers('{"c0": 0.2}', QUESTIONS)
    assert set(partial) == {"c0"}                             # 取不到的题省略


def test_registry_is_config_driven(tmp_path, monkeypatch):
    monkeypatch.delenv("CE_TEACHERS_FILE", raising=False)
    monkeypatch.delenv("CE_TEACHERS", raising=False)
    assert set(T.load_specs()) == {"jev"}                          # 代码里只内置系统自己的判官
    f = tmp_path / "teachers.json"
    f.write_text(json.dumps([{"name": "laya-local", "kind": "system_one", "url": "http://127.0.0.1:8000/v1",
                              "model": "laya", "concurrency": 8},
                             {"name": "glm-5.3", "kind": "openai", "url": "https://x/v4", "model": "glm-5.3",
                              "key_env": "GLM_API_KEY"},
                             {"name": "claude", "kind": "anthropic", "url": "https://api.anthropic.com",
                              "model": "claude-sonnet-5", "key_env": "ANTHROPIC_API_KEY"}]), encoding="utf-8")
    monkeypatch.setenv("CE_TEACHERS_FILE", str(f))
    monkeypatch.setenv("CE_TEACHERS", json.dumps([{"name": "glm-5.3", "kind": "openai", "url": "https://y/v4",
                                                   "model": "glm-5.3"}]))
    specs = T.load_specs()
    assert specs["laya-local"].concurrency == 8 and specs["glm-5.3"].url == "https://y/v4"   # 内联覆盖文件
    ts = T.load_teachers(["jev", "laya-local", "glm-5.3", "claude"])
    assert isinstance(ts["laya-local"], T.SystemOneTeacher) and isinstance(ts["glm-5.3"], T.OpenAITeacher)
    assert isinstance(ts["claude"], T.AnthropicTeacher)
    with pytest.raises(KeyError):
        T.load_teachers(["nope"])
    monkeypatch.setenv("CE_TEACHERS", json.dumps([{"name": "bad", "kind": "claude", "url": "u", "model": "m"}]))
    with pytest.raises(ValueError):
        T.load_specs()


def _fake_http(monkeypatch, responder):
    calls = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json})
            return Resp(responder(len(calls)))

    async def no_sleep(*a):
        pass
    monkeypatch.setattr(T.httpx, "AsyncClient", Client)
    monkeypatch.setattr(T.asyncio, "sleep", no_sleep)
    return calls


@pytest.mark.asyncio
async def test_anthropic_teacher_protocol(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _fake_http(monkeypatch, lambda n: {
        "content": [{"type": "thinking", "thinking": "先想想 {不是答案}"},
                    {"type": "text", "text": '{"c0": 0.8, "c1": 0.1, "best": {"c0": 0.9, "none": 0.1}}'}],
        "usage": {"input_tokens": 100, "output_tokens": 20}})
    spec = T.TeacherSpec(name="claude", kind="anthropic", url="https://api.anthropic.com", model="claude-sonnet-5",
                         key_env="ANTHROPIC_API_KEY", extra={"thinking": {"type": "enabled", "budget_tokens": 4000}})
    ans = await T.AnthropicTeacher(spec).answer("state", QUESTIONS)
    call = calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-ant-test" and call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["json"]["system"] and call["json"]["messages"][0]["role"] == "user"
    assert "temperature" not in call["json"]                       # 开思考时不传温度
    assert ans.answers["c0"]["noul"] == 0.8 and ans.usage["total_tokens"] == 120
    for url, want in (("https://open.bigmodel.cn/api/anthropic", "https://open.bigmodel.cn/api/anthropic/v1/messages"),
                      ("https://x/v1", "https://x/v1/messages"), ("https://x/v1/messages", "https://x/v1/messages")):
        assert T.AnthropicTeacher(T.TeacherSpec(name="a", kind="anthropic", url=url, model="m"))._url() == want


@pytest.mark.asyncio
async def test_chat_teacher_retries_unparsable(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.text}}], "usage": {"total_tokens": 10}}

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append(json)
            return Resp("抱歉" if len(calls) == 1 else '{"c0": 0.8, "c1": 0.1, "best": {"c0": 0.9, "none": 0.1}}')

    async def no_sleep(*a):
        pass
    monkeypatch.setattr(T.httpx, "AsyncClient", Client)
    monkeypatch.setattr(T.asyncio, "sleep", no_sleep)
    spec = T.TeacherSpec(name="t", kind="openai", url="https://x", model="m", extra={"thinking": {"type": "enabled"}})
    ans = await T.OpenAITeacher(spec).answer("state", QUESTIONS)
    assert len(calls) == 2 and ans.answers["c0"]["noul"] == 0.8 and ans.usage == {"total_tokens": 10}
    assert calls[0]["thinking"] == {"type": "enabled"} and calls[0]["temperature"] == 0


# ── 判定记录 / 导出 / 批量标注 ───────────────────────────────────

from datetime import datetime, timedelta  # noqa: E402

from app.distill import export as X  # noqa: E402
from app.distill import records as R  # noqa: E402


class _E:
    def __init__(self, eid):
        self.id = eid


class _Res:
    def __init__(self, eid, reason="primary", exploratory=False):
        self.enzyme, self.reason, self.exploratory = _E(eid), reason, exploratory


def _trace():
    q = {"c0": {"type": "noul", "instructions": {"rule": {"建议": "A 规则"}, "question": "?"}},
         "c1": {"type": "noul", "instructions": {"rule": {"建议": "B 规则"}, "question": "?"}},
         "best": {"type": "choice", "instructions": "?", "criteria": {"c0": "A", "c1": "B", "none": "无"}}}
    return {"recalled_ids": ["b", "a"], "rerank_raw": [("a", 0.9)], "scored": [("a", 0.8, "relevant")],
            "jev": [("a", 0.9, 0.8), ("b", 0.2, 0.1)], "jev_none": 0.1, "jev_status": "ok", "jev_model": "jev-1.13.0",
            "jev_pool": [["a", 3], ["b", 1]], "jev_request": {"state": "key=sk-abcdefghijklmnopqrstuvwxyz0123 部署失败",
                                                               "questions": q},
            "jev_answers": {"c0": {"noul": 0.9}}, "hint_scores": {"b": 0.7}, "exemplar_votes": {"b": 2},
            "lexical_ids": ["b"], "analogy": [("b", 0.7, 0.8)]}


def test_build_record_captures_state_signals_and_policy():
    rec = R.build_record("d1", "key=sk-abcdefghijklmnopqrstuvwxyz0123 部署失败", _trace(), [_Res("a")],
                         session_id="s1", project="p")
    assert rec["policy"]["judge"] == "jev" and rec["policy"]["model"] == "jev-1.13.0"
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in rec["request"]["state"]       # 入库前脱敏
    a, b = rec["candidates"]
    assert (a["id"], a["version"], a["key"], a["recall_rank"], a["rerank"], a["rescued"]) == ("a", 3, "c0", 2, 0.9, False)
    assert (b["rescued"], b["hint"], b["votes"], b["lexical"], b["analog_q"]) == (True, 0.7, 2, True, 0.8)
    assert rec["injected"] == [{"id": "a", "reason": "primary", "exploratory": False}]
    assert rec["context_hash"] == R.context_hash("key=sk-abcdefghijklmnopqrstuvwxyz0123   部署失败")


def test_build_record_merges_protected_and_supplement_choice_requests():
    trace = _trace()
    trace["jev_pool"] = [["a", 3], ["b", 1], ["c", 2]]
    trace["jev"] = [("a", .9, .8), ("b", .2, .1), ("c", .7, .9)]
    trace["jev_request"]["questions"] = {"c0": {"type": "noul"}, "best": {"type": "choice"}}
    trace["jev_answers"] = {"c0": {"noul": .9}, "best": {"probabilities": {"c0": .8}}}
    trace["jev_supplement_request"] = {"state": "ctx", "questions": {"c2": {"type": "noul"}}}
    trace["jev_supplement_answers"] = {"c2": {"noul": .7}}
    rec = R.build_record("d-supp", "ctx", trace, [_Res("a")])
    assert [c["id"] for c in rec["candidates"]] == ["a", "b", "c"]
    assert rec["candidates"][2]["q"] == .9
    assert set(rec["request"]["questions"]) == {"c0", "best", "c2"}
    assert set(rec["answers"]) == {"c0", "best", "c2"}


def _decision(did, t, session="s1", ctx="h1"):
    rec = R.build_record(did, "ctx", _trace(), [_Res("a")], session_id=session)
    rec["created_at"], rec["context_hash"] = t, ctx
    return rec


def test_resolve_outcomes_by_id_context_and_session():
    t0 = datetime(2026, 9, 24, 12)
    d1, d2 = _decision("d1", t0), _decision("d2", t0 + timedelta(minutes=5), ctx="h2")
    outs = [{"decision_id": "d1", "session_id": "", "enzyme_id": "a", "outcome": "gold", "created_at": t0},
            {"decision_id": "", "session_id": "s1", "enzyme_id": "b", "outcome": "revealed",
             "context_hash": "h1", "created_at": t0 + timedelta(hours=1)},
            {"decision_id": "", "session_id": "s1", "enzyme_id": "a", "outcome": "helped",
             "context_hash": "", "created_at": t0 + timedelta(hours=1)},
            {"decision_id": "", "session_id": "other", "enzyme_id": "a", "outcome": "helped", "created_at": t0}]
    got = X.resolve_outcomes([d1, d2], outs)
    assert [o["outcome"] for o in got["d1"]] == ["gold", "revealed"]   # 情境哈希对回 d1
    assert [o["outcome"] for o in got["d2"]] == ["helped"]             # 同会话含该记忆的最近一次
    assert "other" not in {o["session_id"] for v in got.values() for o in v}


def test_labels_closed_vs_open_world():
    rec = _decision("d1", datetime(2026, 9, 24))
    labels, best, closed = X.labels_for(rec, [{"enzyme_id": "a", "outcome": "gold"}])
    assert closed and labels == {"c0": 1, "c1": 0} and best == "c0"
    labels, best, closed = X.labels_for(rec, [{"enzyme_id": "b", "outcome": "contradicted"}])
    assert not closed and labels == {"c1": 0} and best == ""
    labels, best, closed = X.labels_for(rec, [{"enzyme_id": "*", "outcome": "silent"}])
    assert closed and labels == {"c0": 0, "c1": 0} and best == "none"


def test_export_formats():
    rec = _decision("d1", datetime(2026, 9, 24))
    outs = [{"enzyme_id": "a", "outcome": "gold"}]
    teachers = {"glm-5.3": {"c0": {"noul": 0.95}, "c1": {"noul": 0.1},
                            "best": {"probabilities": {"c0": 0.9, "c1": 0.05, "none": 0.05}}}}
    [arena] = X.to_formats(rec, outs, teachers, "arena", [])
    assert arena["positive"] == ["c0"] and arena["expected"] == ["a"] and set(arena["logged"]) == {"jev-1.13.0", "glm-5.3"}
    [laya] = X.to_formats(rec, outs, teachers, "laya", ["glm-5.3"])
    gold, qs = json.loads(laya["gold"]), json.loads(laya["questions"])
    assert gold["c0"] == {"label": "true", "probabilities": {"true": 1, "false": 0}}      # 真实标签优先
    assert gold["best"]["label"] == "c0" and gold["best"]["probabilities"]["c0"] == 1.0
    assert set(qs) == {"c0", "c1", "best"} and json.loads(laya["state"]) == rec["request"]["state"]
    [soft] = X.to_formats(rec, [], teachers, "laya", ["glm-5.3"], mix=0.0)                # 无标签 → 老师软标签
    assert json.loads(soft["gold"])["c0"]["probabilities"]["true"] == 0.95
    [mixed] = X.to_formats(rec, outs, teachers, "laya", ["glm-5.3"], mix=0.5)
    assert abs(json.loads(mixed["gold"])["c0"]["probabilities"]["true"] - 0.975) < 1e-9
    [short] = X.to_formats(rec, outs, teachers, "laya", ["glm-5.3"], max_options=1)       # 短名单保住正确答案
    assert set(json.loads(short["questions"])["best"]["criteria"]) == {"c0", "none"}
    [sft] = X.to_formats(rec, outs, teachers, "sft", [])
    assert json.loads(sft["messages"][-1]["content"]) == {"inject": ["c0"]} and sft["meta"]["target"] == "labels"
    [sft_t] = X.to_formats(rec, [], teachers, "sft", ["glm-5.3"])       # 无标签 → 用老师的判定
    assert json.loads(sft_t["messages"][-1]["content"]) == {"inject": ["c0"]} and sft_t["meta"]["target"] == "teacher:glm-5.3"
    [pair] = X.to_formats(rec, outs, teachers, "pairs", [])
    assert "A 规则" in pair["chosen"] and "B 规则" in pair["rejected"]
    [ret] = X.to_formats(rec, outs, teachers, "retrieval", [])
    assert ret["positive"] == ["建议: A 规则"] and ret["negative"] == ["建议: B 规则"]


@pytest.mark.asyncio
async def test_label_stores_teacher_answers(monkeypatch):
    from app.distill import label as L

    class PG:
        def __init__(self):
            self.saved = {}

        async def fetch_decisions(self, since=None, limit=1000, after_id="", unlabeled_by=""):
            return [{"id": "d1", "request": {"state": "s", "questions": {"c0": {"type": "noul"}}}},
                    {"id": "d2", "request": {"state": "s", "questions": {"c0": {"type": "noul"}}}}]

        async def upsert_teacher_label(self, did, teacher, answers, usage=None):
            self.saved[did] = (teacher, answers)

    class Teacher:
        spec = T.TeacherSpec(name="fake", kind="openai", url="x", model="m", concurrency=2)

        async def answer(self, state, questions):
            return T.TeacherAnswer({"c0": {"type": "noul", "noul": 0.7}}, {"total_tokens": 5})

    monkeypatch.setattr(T, "load_teachers", lambda names=None, path=None: {"fake": Teacher()})
    pg = PG()
    stats = await L.label("fake", pg=pg)
    assert stats == {"teacher": "fake", "picked": 2, "ok": 2, "failed": 0, "tokens": 10}
    assert pg.saved["d1"] == ("fake", {"c0": {"type": "noul", "noul": 0.7}})


# ── System 1 网关 / 影子判官 ─────────────────────────────────────

from app.distill import gateway as G  # noqa: E402

GQ = {"billing": {"type": "noul", "instructions": "是不是账单问题?"},
      "dept": {"type": "choice", "instructions": "哪个部门?", "criteria": {"billing": "账单", "tech": "技术"}}}


class _Judge:
    def __init__(self, noul, dept, fail=False):
        self.spec = T.TeacherSpec(name="x", kind="openai", url="u", model="m")
        self.noul, self.dept, self.fail, self.calls = noul, dept, fail, 0

    async def answer(self, state, questions):
        self.calls += 1
        if self.fail:
            return T.TeacherAnswer(None, {}, 0.0, "boom")
        return T.TeacherAnswer({"billing": {"type": "noul", "noul": self.noul},
                                "dept": {"type": "choice", "probabilities": self.dept}}, {"total_tokens": 1})


@pytest.mark.asyncio
async def test_gateway_escalates_when_student_unsure(monkeypatch):
    student, teacher = _Judge(0.55, {"billing": 0.5, "tech": 0.5}), _Judge(0.97, {"billing": 0.9, "tech": 0.1})
    monkeypatch.setattr(G, "_TEACHERS", {"student": student, "teacher": teacher})
    monkeypatch.setenv("CE_SYSTEMONE_ROUTE", "student,teacher")
    res = await G.answer("我被扣了两次钱", GQ)
    assert res["model"] == "teacher" and [p["judge"] for p in res["path"]] == ["student", "teacher"]
    assert set(res["tried"]) == {"student", "teacher"}             # 学生的作答也留作标注
    student.noul, student.dept = 0.95, {"billing": 0.8, "tech": 0.2}   # 学生有把握 → 不升级
    res = await G.answer("我被扣了两次钱", GQ)
    assert res["model"] == "student" and teacher.calls == 1
    res = await G.answer("x", GQ, model="teacher")                 # 指定判官
    assert res["model"] == "teacher"


@pytest.mark.asyncio
async def test_gateway_falls_back_and_fails_cleanly(monkeypatch):
    monkeypatch.setattr(G, "_TEACHERS", {"a": _Judge(0.5, {"billing": 0.5}), "b": _Judge(0, {}, fail=True)})
    monkeypatch.setenv("CE_SYSTEMONE_ROUTE", "a,b")
    res = await G.answer("x", GQ)                                  # 最后一位失败 → 退回最后一个成功的
    assert res["model"] == "a"
    monkeypatch.setattr(G, "_TEACHERS", {"b": _Judge(0, {}, fail=True)})
    monkeypatch.setenv("CE_SYSTEMONE_ROUTE", "b")
    with pytest.raises(G.NoJudgeAvailable):
        await G.answer("x", GQ)


@pytest.mark.asyncio
async def test_gateway_route_records_and_feedback(monkeypatch):
    from app.routers import systemone as S

    class PG:
        def __init__(self):
            self.decisions, self.labels, self.outcomes = [], [], []

        async def insert_decision(self, rec):
            self.decisions.append(rec)

        async def upsert_teacher_label(self, did, teacher, answers, usage=None):
            self.labels.append((did, teacher))

    class Eng:
        def __init__(self):
            self.pg, self.tasks = PG(), []

        def spawn(self, coro):
            self.tasks.append(coro)

        async def record_outcome(self, eid, outcome, source="", **kw):
            self.pg.outcomes.append((eid, outcome, source, kw.get("decision_id")))

    monkeypatch.setattr(G, "_TEACHERS", {"jev": _Judge(0.97, {"billing": 0.9, "tech": 0.1})})
    monkeypatch.setenv("CE_SYSTEMONE_ROUTE", "jev")
    eng = Eng()
    out = await S.systemone(S.SystemOneRequest(state={"body": "key=sk-abcdefghijklmnopqrstuvwxyz0123 重复扣费"},
                                               questions=GQ, agent="support-bot"), eng)
    assert out["model"] == "jev" and out["answers"]["billing"]["noul"] == 0.97 and out["decision_id"]
    for coro in eng.tasks:
        await coro
    [rec] = eng.pg.decisions
    assert rec["policy"]["source"] == "gateway" and rec["project"] == "support-bot"
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in rec["request"]["state"]
    assert eng.pg.labels == [(out["decision_id"], "jev")]
    await S.systemone_feedback(S.SystemOneFeedback(decision_id=out["decision_id"],
                                                   labels={"billing": True, "dept": "billing"}), eng)
    assert eng.pg.outcomes == [("billing", "true", "gateway:agent", out["decision_id"]),
                               ("dept", "choice:billing", "gateway:agent", out["decision_id"])]
    with pytest.raises(Exception):
        await S.systemone(S.SystemOneRequest(questions={}), eng)


def test_gateway_is_machine_data_plane():
    from app.security import machine_api_access_allowed
    assert machine_api_access_allowed("POST", "/api/v1/systemone")
    assert machine_api_access_allowed("POST", "/api/v1/systemone/feedback")


def test_shadow_judges_env(monkeypatch):
    assert R.shadow_judges() == []
    monkeypatch.setenv("CE_SHADOW_JUDGES", "laya-local, glm-5.3")
    assert R.shadow_judges("prime") == ["laya-local", "glm-5.3"]
    assert R.shadow_judges("eval") == [] and R.shadow_judges("stream") == []   # 评测不跑影子
    monkeypatch.setenv("CE_SHADOW_RATE", "0")
    assert R.shadow_judges("prime") == []


def test_export_gateway_question_labels():
    rec = {"id": "g1", "request": {"state": "重复扣费", "questions": GQ}, "answers": None, "candidates": [],
           "injected": [], "policy": {"source": "gateway", "judge": "jev"}}
    teachers = {"jev": {"billing": {"noul": 0.8}, "dept": {"probabilities": {"billing": 0.7, "tech": 0.3}}}}
    outs = [{"enzyme_id": "billing", "outcome": "false"}, {"enzyme_id": "dept", "outcome": "choice:tech"}]
    [row] = X.to_formats(rec, outs, teachers, "laya", ["jev"])
    gold = json.loads(row["gold"])
    assert gold["billing"]["label"] == "false" and gold["dept"]["label"] == "tech"   # 回传的真实结果压过老师
    [soft] = X.to_formats(rec, [], teachers, "laya", ["jev"])
    assert json.loads(soft["gold"])["dept"]["label"] == "billing"                    # 没回传 → 老师软标签


@pytest.mark.asyncio
async def test_engine_shadow_answer_records_label():
    from app.config import Config
    from app.core.engine import Engine

    class PG:
        saved = []

        async def upsert_teacher_label(self, did, teacher, answers, usage=None):
            self.saved.append((did, teacher, answers))
    eng = Engine(Config(), PG(), qdrant=None)
    eng._shadow_teachers["student"] = _Judge(0.9, {"billing": 1.0})
    await eng._shadow_answer("student", "d9", {"state": "s", "questions": GQ})
    assert PG.saved and PG.saved[0][:2] == ("d9", "student")


def test_export_kd_for_generative_students():
    rec = _decision("d1", datetime(2026, 9, 24))
    outs = [{"enzyme_id": "a", "outcome": "gold"}]
    teachers = {"glm-5.3": {"c0": {"noul": 0.95}, "c1": {"noul": 0.1},
                            "best": {"probabilities": {"c0": 0.9, "c1": 0.05, "none": 0.05}}}}
    rows = X.to_formats(rec, outs, teachers, "kd", ["glm-5.3"], mix=0.2)
    by = {r["meta"]["qid"]: r for r in rows}
    assert set(by) == {"c0", "c1", "best"}
    c0 = by["c0"]
    assert c0["label"] == "A" and c0["messages"][-1]["content"] == "A" and c0["options"] == ["A", "B"]
    assert abs(c0["target"]["A"] - (0.8 * 1 + 0.2 * 0.95)) < 1e-9          # 硬标签混入老师概率
    best = by["best"]
    assert best["label"] == "A" and set(best["target"]) == {"A", "B", "C"}   # c0 / c1 / none → 字母
    assert "A) A\nB) B\nC) 无" in best["messages"][1]["content"]
    assert "A) 是" in c0["messages"][1]["content"] and "建议: A 规则" in c0["messages"][1]["content"]


# ── 校准 / 合成 / 上岗门槛 ──────────────────────────────────────

from app.distill import calibrate as C  # noqa: E402
from app.distill import gate as GT  # noqa: E402


def test_platt_fixes_overconfident_teacher():
    import random
    rng = random.Random(0)
    pairs = []
    for _ in range(600):                      # 真实命中率 70% 的样本,老师却一律报 0.97
        pairs.append((0.97, 1 if rng.random() < 0.7 else 0))
    for _ in range(600):                      # 真实命中率 10%,老师报 0.3
        pairs.append((0.3, 1 if rng.random() < 0.1 else 0))
    params = C.fit_platt(pairs)
    hi = C._sigmoid(params["a"] * C._logit(0.97) + params["b"])
    lo = C._sigmoid(params["a"] * C._logit(0.3) + params["b"])
    assert 0.6 < hi < 0.8 and 0.05 < lo < 0.2
    assert C.fit_platt([(0.5, 1)] * 5) == {"a": 1.0, "b": 0.0}       # 样本太少 → 恒等


def test_temperature_and_apply():
    items = [({"c0": 0.99, "c1": 0.01}, "c0")] * 6 + [({"c0": 0.99, "c1": 0.01}, "c1")] * 6
    assert C.fit_temperature(items)["T"] > 1.0                      # 一半答错却 0.99 → 需要升温
    ans = {"c0": {"noul": 0.9}, "best": {"probabilities": {"c0": 0.9, "c1": 0.1}}}
    out = C.apply(ans, {"noul": {"a": 1.0, "b": -1.0}, "choice": {"T": 2.0}})
    assert out["c0"]["noul"] < 0.9 and out["best"]["probabilities"]["c0"] < 0.9 and out["best"]["choice"] == "c0"
    assert C.apply(ans, None) is ans


def test_combine_and_disagreement():
    sets = {"a": {"c0": {"noul": 0.9}, "best": {"probabilities": {"c0": 0.8, "none": 0.2}}},
            "b": {"c0": {"noul": 0.1}, "best": {"probabilities": {"c0": 0.2, "none": 0.8}}}}
    mix = C.combine(sets)
    assert abs(mix["c0"]["noul"] - 0.5) < 1e-6 and abs(mix["best"]["probabilities"]["c0"] - 0.5) < 1e-6
    heavy = C.combine(sets, weights={"a": 3.0, "b": 1.0})
    assert abs(heavy["c0"]["noul"] - 0.75) < 1e-6        # logit 加权:(3·2.197 − 2.197)/4 → σ = 0.75
    assert abs(C.disagreement(sets, "c0") - 0.8) < 1e-9 and abs(C.disagreement(sets, "best") - 0.6) < 1e-9


def test_fit_teacher_from_arena_decisions():
    decisions = [{"id": f"d{i}", "kind": "positive", "candidates": ["x", "y"], "positive": ["c0"]} for i in range(30)]
    answers = {d["id"]: {"c0": {"noul": 0.6}, "c1": {"noul": 0.6},
                         "best": {"probabilities": {"c0": 0.6, "c1": 0.4, "none": 0.0}}} for d in decisions}
    params = C.fit_teacher(decisions, answers)
    assert params["n_noul"] == 60 and params["n_choice"] == 30 and params["ece_after"] <= params["ece_before"]


def test_gate_verdict_and_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CE_JUDGE_REGISTRY", str(tmp_path / "registry.json"))
    inc = {"sel_recall": 0.5, "sel_precision": 0.8, "sel_silence": 1.0, "noul_auc": 0.96}
    better = {"sel_recall": 0.55, "sel_precision": 0.8, "sel_silence": 1.0, "noul_auc": 0.955}
    worse = {"sel_recall": 0.5, "sel_precision": 0.7, "sel_silence": 0.95, "noul_auc": 0.97}
    assert GT.verdict(better, inc)["passed"]
    v = GT.verdict(worse, inc)
    assert not v["passed"] and not v["checks"]["selection_f1"]["passed"] and not v["checks"]["silence"]["passed"]
    entry = GT.record("laya-v1", "jev", "jsonl:exam", v, {"laya-v1": worse, "jev": inc})
    assert entry["status"] == "failed" and len(entry["history"]) == 1
    entry = GT.record("laya-v1", "jev", "jsonl:exam", GT.verdict(better, inc), {})
    assert entry["status"] == "passed" and len(entry["history"]) == 2


@pytest.mark.asyncio
async def test_gate_run_uses_logged_answers(tmp_path, monkeypatch):
    monkeypatch.setenv("CE_JUDGE_REGISTRY", str(tmp_path / "registry.json"))
    good = {"c0": {"noul": 0.95}, "c1": {"noul": 0.05}, "best": {"probabilities": {"c0": 0.9, "c1": 0.05, "none": 0.05}}}
    bad = {"c0": {"noul": 0.1}, "c1": {"noul": 0.9}, "best": {"probabilities": {"c0": 0.05, "c1": 0.9, "none": 0.05}}}
    rows = [{"id": f"d{i}", "domain": "x", "kind": "positive", "state": "s", "questions": {},
             "candidates": ["a", "b"], "positive": ["c0"], "expected": ["a"], "floor_ok": ["a", "b"],
             "logged": {"cand": good, "inc": bad}} for i in range(5)]
    f = tmp_path / "exam.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = await GT.run("cand", "inc", f"jsonl:{f}", tmp_path / "cache")
    assert out["passed"] and out["status"] == "passed" and out["exam_size"] == 5
    out = await GT.run("inc", "cand", f"jsonl:{f}", tmp_path / "cache")
    assert not out["passed"]


@pytest.mark.asyncio
async def test_export_from_archive_without_db(tmp_path):
    """训练机上只读归档(不连库)导出;同一主键多次归档只取最新一份。"""
    import gzip
    rec = _decision("d1", datetime(2026, 9, 24, 12))
    rec["created_at"] = "2026-09-24T12:00:00+00:00"
    dump = lambda name, rows: gzip.open(tmp_path / name, "at", encoding="utf-8").write(  # noqa: E731
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    dump("decision_log-2026-09-24.jsonl.gz", [rec])
    dump("decision_log-2026-09-25.jsonl.gz", [rec])                                   # 重复归档
    dump("decision_outcomes-2026-09-24.jsonl.gz", [{"id": 1, "decision_id": "d1", "session_id": "", "enzyme_id": "a",
                                                    "outcome": "gold", "source": "eval", "context_hash": "",
                                                    "created_at": "2026-09-24T12:00:01+00:00"}])
    dump("teacher_labels-2026-09-24.jsonl.gz", [{"decision_id": "d1", "teacher": "glm-5.3",
                                                 "answers": {"c0": {"noul": 0.9}}, "created_at": "2026-09-24T13:00:00+00:00"}])
    out = tmp_path / "laya.jsonl"
    stats = await X.export("laya", out, archive=str(tmp_path), use_db=False, prefer=["glm-5.3"])
    assert stats["decisions"] == 1 and stats["rows"] == 1
    row = json.loads(out.read_text(encoding="utf-8"))
    assert json.loads(row["gold"])["c0"]["label"] == "true" and row["meta"]["soft_teacher"] == "glm-5.3"


@pytest.mark.asyncio
async def test_engine_drain_waits_for_background_writes():
    """关停前 drain:后台写入(判定记录等)要落完再关连接池;卡住的写入不无限等。"""
    from app.core.engine import Engine
    eng = Engine.__new__(Engine)
    eng._bg = set()
    done = []

    async def write():
        await asyncio.sleep(0.05)
        done.append(1)

    async def stuck():
        await asyncio.sleep(3600)

    eng._background(write())
    await eng.drain(timeout=5)
    assert done == [1]
    eng._background(stuck())
    await eng.drain(timeout=0.05)          # 超时即返回
    assert len(eng._bg) == 1
    next(iter(eng._bg)).cancel()
