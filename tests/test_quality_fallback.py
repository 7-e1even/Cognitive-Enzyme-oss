"""质量门三层退路:语言模型 → 判官(Jev score 题)→ 规则;以及对话记录的长度上限。"""

import importlib.util
import json
from pathlib import Path

import pytest

from app import llm_client, typesafe_client
from app.config import TypeSafeConfig
from app.learning import quality_gate as qg
from app.learning.transcript import clip_transcript

TS = TypeSafeConfig(api_key="k", api_url="http://ts")
GOOD = "Spring Boot Actuator 默认暴露 /actuator/env,生产环境应把 management.endpoints.web.exposure.include 限制为 health,info"


@pytest.fixture
def models(monkeypatch):
    state = {"llm": None, "judge": None, "judge_calls": 0, "questions": None}

    async def fake_chat_json(system, user, config=None, timeout=30.0):
        return state["llm"]

    async def fake_system_one(s, questions, config, timeout=10.0):
        state["judge_calls"] += 1
        state["questions"] = questions
        return state["judge"]

    monkeypatch.setattr(llm_client, "chat_json", fake_chat_json)
    monkeypatch.setattr(typesafe_client, "system_one", fake_system_one)
    return state


def _judge_answers(levels: dict[str, float]) -> dict:
    return {"answers": {d: {"type": "score", "score": v} for d, v in levels.items()}}


@pytest.mark.asyncio
async def test_llm_result_is_used_when_available(models):
    models["llm"] = {"scores": {d: 4 for d in qg.DIMENSIONS}, "reason": "ok"}
    r = await qg.assess_quality(GOOD, "审计配置", typesafe_config=TS)
    assert r.decision == qg.PASS and r.score == 4.0 and models["judge_calls"] == 0


@pytest.mark.asyncio
async def test_judge_scores_when_llm_fails(models):
    models["judge"] = _judge_answers({"specificity": 3.4, "novelty": 3.0, "accuracy": 3.2,
                                      "universality": 2.4, "actionability": 3.0})
    r = await qg.assess_quality(GOOD, "审计配置", [{"then": "已有的一条"}], typesafe_config=TS)
    assert r.dimensions["specificity"] == pytest.approx(4.4)       # 0~4 档 → 1~5 分
    assert r.score == pytest.approx(4.0) and r.decision == qg.PASS and "判官" in r.reason
    q = models["questions"]["novelty"]
    assert q["type"] == "score" and len(q["criteria"]) == 5          # 五档,从低到高


@pytest.mark.asyncio
async def test_judge_never_refines(models):
    models["judge"] = _judge_answers({d: 2.0 for d in qg.DIMENSIONS})   # 3 分:改写区间,但判官不写改写稿
    r = await qg.assess_quality(GOOD, "审计配置", typesafe_config=TS)
    assert r.decision == qg.QUARANTINE and not r.improved_insight


@pytest.mark.asyncio
async def test_rules_when_llm_and_judge_both_fail(models):
    r = await qg.assess_quality(GOOD, "审计配置", typesafe_config=TS)
    assert r.reason.startswith("规则检查") and models["judge_calls"] == 1
    r = await qg.assess_quality(GOOD, "审计配置")                       # 没配判官:和原来一样直接用规则
    assert r.reason.startswith("规则检查") and models["judge_calls"] == 1


def test_clip_keeps_start_and_most_of_the_end():
    assert clip_transcript("短对话", limit=100) == "短对话"
    text = "开头交代任务" + "x" * 5000 + "最后这样解决"
    out = clip_transcript(text, limit=1000)
    assert out.startswith("开头交代任务") and out.endswith("最后这样解决")
    assert "中间省略" in out and len(out) < 1100


def test_clip_default_comes_from_runtime_setting(monkeypatch):
    monkeypatch.setenv("CE_CAPTURE_CHARS", "2000")
    assert "中间省略" in clip_transcript("y" * 3000)
    monkeypatch.setenv("CE_CAPTURE_CHARS", "200000")
    assert clip_transcript("y" * 3000) == "y" * 3000


def _load_hook():
    path = Path(__file__).resolve().parent.parent / "hooks" / "ce_capture_hook.py"
    spec = importlib.util.spec_from_file_location("ce_capture_hook", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_keeps_first_turns_and_recent_ones(tmp_path, monkeypatch):
    hook = _load_hook()
    rows = [{"type": "user", "message": {"role": "user", "content": f"第{i}轮"}} for i in range(10)]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    monkeypatch.setattr(hook, "MAX_TURNS", 5)
    digest = hook._read_transcript(str(path))
    assert [line for line in digest.split("\n\n")] == ["[user] 第0轮", "[user] 第1轮", "[user] 第7轮",
                                                       "[user] 第8轮", "[user] 第9轮"]
    monkeypatch.setattr(hook, "MAX_TURNS", 400)
    monkeypatch.setattr(hook, "MAX_CHARS", 100)      # 全文 108 字:开头留 10 字,结尾留 90 字
    digest = hook._read_transcript(str(path))
    assert digest.startswith("[user] 第0") and digest.endswith("第9轮") and "中间省略" in digest
