"""判官请求体积控制 · judge_applicability 之外的 system_one 调用方:

- matching/route.pick_domains      领域分流单选(state = 提问,选项 = 领域名:描述 + none)
- learning/domains.judge_domain    领域归类单选(state = {"memory": 规则},选项同上)
- learning/quality_gate._jev_assess 质量兜底(state = {insight, context, 相似记忆},5 道 score)
- learning/dedup.judge_duplicate   语义去重 noul(state = {规则A, 规则B})
- typesafe_client.pick_context     经验样本归属单选(state = {"memory": 规则},选项 = 各条请求)
- model_registry.probe_model       判官连通性探测(自定义 prompt 作题面)

bocha-jev-v1 单次请求 expanded_input_tokens ≤ 32768,超出直接 422、整次作废。
每个调用方都按 judge.token_budget 估算截断;只裁内容,题面措辞逐字不动。
"""
import json

import pytest

import app.typesafe_client as tc
from app import settings
from app.config import TypeSafeConfig
from app.core import Enzyme, Pattern
from app.learning import dedup, domains, quality_gate
from app.matching import route
from app.typesafe_client import _estimate_tokens

TS = TypeSafeConfig(api_key="k", api_url="https://ts.invalid/v1")


def _chars(state) -> int:
    return len(state) if isinstance(state, str) else len(json.dumps(state, ensure_ascii=False))


def _request_est(state: dict, questions: dict) -> float:
    """与 fit_options 同口径:逐题数 = 题数,R = 各题 criteria 的 JSON 长度合计。"""
    R = sum(len(json.dumps(q["criteria"], ensure_ascii=False)) for q in questions.values())
    return _estimate_tokens(len(questions), _chars(state), R)


def _fat_domains(n=60):
    return [{"name": f"domain-{i:02d}-" + "n" * 200, "description": "d" * 500} for i in range(n)]


def _fake(captured, answer_key):
    """替身 system_one:记下请求,按实际收到的 criteria 给均匀概率(键以发出去的为准)。"""

    async def fake(state, questions, config, timeout=0):
        captured["state"], captured["questions"] = state, questions
        criteria = questions[answer_key]["criteria"]
        return {"model": "jev-test",
                "answers": {answer_key: {"probabilities": {k: 1 / len(criteria) for k in criteria}}}}

    return fake


# ── matching/route.pick_domains ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_domains_drops_tail_options_to_fit_budget(monkeypatch):
    """60 个胖领域(名 210 字、描述 500 字):描述截到 80,选项从尾部丢到不超预算,前缀不动。"""
    monkeypatch.setattr(route, "_cache", None)
    fat = _fat_domains()
    captured = {}

    class PG:
        async def routable_domains(self):
            return fat

    monkeypatch.setattr(route, "system_one", _fake(captured, "route"))
    out = await route.pick_domains("x" * 5000, PG(), TS)

    assert out is not None and len(fat) == 60                       # 缓存的领域表不被原地改动
    q = captured["questions"]["route"]
    crit = q["criteria"]
    kept = [k for k in crit if k != "none"]
    assert 0 < len(kept) < 60 and kept == [f"d{i}" for i in range(len(kept))]   # 前缀、顺序不动
    assert list(crit)[-1] == "none" and crit["none"] == route._NONE
    assert q["instructions"] == route._QUESTION                        # 题面逐字不动
    assert captured["state"] == "x" * tc._STATE_MAX                    # 1500(原 2000)
    for i, k in enumerate(kept):
        assert crit[k] == f"{fat[i]['name']}:{'d' * tc._OPTION_DESC_MAX}"
    budget = settings.get("judge.token_budget")
    assert _request_est(captured["state"], captured["questions"]) <= budget
    # 前缀最长:再留一个选项就超预算
    nxt = dict(crit)
    i = len(kept)
    nxt[f"d{i}"] = f"{fat[i]['name']}:{fat[i]['description'][:tc._OPTION_DESC_MAX]}"
    assert _estimate_tokens(1, _chars(captured["state"]), len(json.dumps(nxt, ensure_ascii=False))) > budget


@pytest.mark.asyncio
async def test_pick_domains_small_input_byte_identical(monkeypatch):
    """小请求:state 原文、选项「名:描述」原文、题面与 none 逐字不动。"""
    monkeypatch.setattr(route, "_cache", None)
    captured = {}

    class PG:
        async def routable_domains(self):
            return [{"name": "legal", "description": "判例"}, {"name": "security", "description": "漏洞"}]

    monkeypatch.setattr(route, "system_one", _fake(captured, "route"))
    await route.pick_domains("合同违约怎么判", PG(), TS)
    assert captured["state"] == "合同违约怎么判"
    assert captured["questions"]["route"]["criteria"] == {
        "d0": "legal:判例", "d1": "security:漏洞", "none": route._NONE}


@pytest.mark.asyncio
async def test_pick_domains_zero_budget_disables_trimming(monkeypatch):
    monkeypatch.setenv("CE_JEV_TOKEN_BUDGET", "0")
    monkeypatch.setattr(route, "_cache", None)
    captured = {}

    class PG:
        async def routable_domains(self):
            return _fat_domains()

    monkeypatch.setattr(route, "system_one", _fake(captured, "route"))
    await route.pick_domains("q", PG(), TS)
    assert len(captured["questions"]["route"]["criteria"]) == 61        # 60 领域 + none,一个不丢


# ── learning/domains.judge_domain ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_judge_domain_caps_rule_and_drops_tail_options(monkeypatch):
    fat = _fat_domains()
    captured = {}
    monkeypatch.setattr(domains, "system_one", _fake(captured, "domain"))
    rule = {"情境": "c" * 3000, "内容": "i" * 3000}
    out = await domains.judge_domain(rule, fat, TS)

    assert out is not None and len(fat) == 60                          # 调用方列表不被原地改动
    mem = captured["state"]["memory"]
    assert mem == {"情境": "c" * tc._RULE_THEN_MAX, "内容": "i" * tc._RULE_THEN_MAX}
    assert rule["内容"] == "i" * 3000                                    # 调用方的 dict 不动
    q = captured["questions"]["domain"]
    crit = q["criteria"]
    kept = [k for k in crit if k != "none"]
    assert 0 < len(kept) < 60 and kept == [f"d{i}" for i in range(len(kept))]
    assert crit["none"] == domains._NONE_OPTION and q["instructions"] == domains._ASSIGN_QUESTION
    assert _request_est(captured["state"], captured["questions"]) <= settings.get("judge.token_budget")
    assert out["closest"] == fat[0]["name"]                            # 均匀概率 → 取第一个选项


# ── learning/quality_gate._jev_assess ───────────────────────────────────────

@pytest.mark.asyncio
async def test_jev_assess_caps_insight(monkeypatch):
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["state"], captured["questions"] = state, questions
        return {"answers": {d: {"score": 2} for d in quality_gate._JUDGE_RUBRIC}}

    monkeypatch.setattr(tc, "system_one", fake)        # _jev_assess 是调用点处的惰性 import
    res = await quality_gate._jev_assess("i" * 5000, "c" * 2000, [{"then": "t" * 300}] * 5, TS)

    assert res is not None
    assert captured["state"]["insight"] == "i" * 1200
    assert captured["state"]["context"] == "c" * 300
    assert captured["state"]["已有相似记忆"] == ["t" * 80] * 5
    assert _request_est(captured["state"], captured["questions"]) <= settings.get("judge.token_budget")
    for d, (question, levels) in quality_gate._JUDGE_RUBRIC.items():   # 题面与评分档逐字不动
        assert captured["questions"][d]["instructions"] == question
        assert captured["questions"][d]["criteria"] == levels


# ── learning/dedup.judge_duplicate ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_judge_duplicate_caps_rule_fields(monkeypatch):
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["state"], captured["questions"] = state, questions
        return {"answers": {"dup": {"noul": 0.9}}}

    monkeypatch.setattr(dedup, "system_one", fake)
    a = Enzyme(id="a", patterns=[Pattern(value="p")], then="t" * 3000, because="b" * 2000)
    b = Enzyme(id="b", patterns=[Pattern(value="q")], then="x" * 3000, because="y" * 2000)
    assert await dedup.judge_duplicate(a, b, TS) == 0.9

    for key in ("规则A", "规则B"):
        rule = captured["state"][key]
        assert list(rule) == ["触发", "建议", "依据"]                  # 键名逐字不动
        assert len(rule["建议"]) == tc._RULE_THEN_MAX and len(rule["依据"]) == tc._RULE_WHY_MAX
    assert captured["state"]["规则A"] == dedup._rule(a)                # 判定缓存哈希与实际发出同口径
    assert a.then == "t" * 3000 and a.because == "b" * 2000            # 存库原文不动
    q = captured["questions"]["dup"]
    assert q["instructions"] == dedup._DUP_QUESTION and q["criteria"] == dedup._DUP_CRITERIA
    assert _request_est(captured["state"], captured["questions"]) <= settings.get("judge.token_budget")


# ── typesafe_client.pick_context ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_context_caps_options_and_returns_full_text(monkeypatch):
    """30 条 3000 字候选情境:选项各截 400、超预算从尾部丢;返回的仍是未截断的原文。"""
    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["state"], captured["questions"] = state, questions
        probs = {k: 0.01 for k in questions["where"]["criteria"]}
        probs["s0"], probs["none"] = 0.9, 0.05
        return {"answers": {"where": {"probabilities": probs}}}

    monkeypatch.setattr(tc, "system_one", fake)
    e = Enzyme(id="e", patterns=[Pattern(value="p")], then="t" * 3000, because="b" * 2000)
    contexts = [f"ctx-{i}-" + "x" * 3000 for i in range(30)]
    got = await tc.pick_context(e, contexts, TS)

    assert got == contexts[0] and len(got) > tc._CONTEXT_OPTION_MAX    # 返回原文,不是截断稿
    mem = captured["state"]["memory"]                                  # v1 键
    assert len(mem["建议"]) == tc._RULE_THEN_MAX and len(mem["依据"]) == tc._RULE_WHY_MAX
    assert e.then == "t" * 3000 and e.because == "b" * 2000
    q = captured["questions"]["where"]
    crit = q["criteria"]
    kept = [k for k in crit if k != "none"]
    assert 0 < len(kept) < 30 and kept == [f"s{i}" for i in range(len(kept))]
    assert all(crit[k] == contexts[int(k[1:])][:tc._CONTEXT_OPTION_MAX] for k in kept)
    assert crit["none"] == "这条记忆对其中任何一条请求都不适用"
    assert q["instructions"].startswith("state 是一条记忆。")          # 题面逐字不动
    assert _request_est(captured["state"], captured["questions"]) <= settings.get("judge.token_budget")
    nxt = dict(crit)
    nxt[f"s{len(kept)}"] = contexts[len(kept)][:tc._CONTEXT_OPTION_MAX]
    assert _estimate_tokens(1, _chars(captured["state"]),
                            len(json.dumps(nxt, ensure_ascii=False))) > settings.get("judge.token_budget")


# ── model_registry.probe_model(judge) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_model_caps_judge_prompt(monkeypatch):
    """连通性探测的自定义题面截到 2000 字——再长的 prompt 也不会把探测打成 422。"""
    from app import model_registry as mr

    captured = {}

    async def fake(state, questions, config, timeout=0):
        captured["state"], captured["questions"] = state, questions
        return {"answers": {"q": {"noul": 0.5}}}

    monkeypatch.setattr(tc, "system_one", fake)        # probe_model 是调用点处的惰性 import
    p = mr.Provider("ts", "TypeSafe", "system_one", "https://ts.invalid/v1", "k")
    m = mr.Model("jev", "ts", "judge", "jev-x")
    out = await mr.probe_model(m, p, prompt="p" * 10000)

    assert out["ok"] and captured["questions"]["q"]["instructions"] == "p" * 2000
    assert captured["questions"]["q"]["criteria"] == {"true": "是编程问题", "false": "不是编程问题"}
    assert _request_est(captured["state"], captured["questions"]) <= settings.get("judge.token_budget")
