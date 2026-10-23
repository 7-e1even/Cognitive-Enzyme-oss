import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import settings
from app.core import Enzyme, Pattern
from app.config import TypeSafeConfig
import app.typesafe_client as tc
from app.matching import pipeline as pl
from tests.eval.diagnostic.protocol import sha
from tests.eval.diagnostic.two_stage import FreshHTTP, restore, scan_ids, summarize


def test_restore_uses_saved_exemplar_similarity_and_best_text(monkeypatch):
    monkeypatch.setitem(settings._values, "exemplar.analogy_k", 1)
    monkeypatch.setitem(settings._values, "exemplar.anti", False)
    bank = [Enzyme(id="a", patterns=[Pattern(value="a")], then="rule", exemplars=["weak", "strong"]),
            Enzyme(id="b", patterns=[Pattern(value="b")], then="rule", exemplars=["middle"])]
    rec = {"exemplar_hits": [
        {"id": "a", "kind": "pos", "score": .1, "text_sha256": sha("weak")},
        {"id": "a", "kind": "pos", "score": .9, "text_sha256": sha("strong")},
        {"id": "b", "kind": "pos", "score": .5, "text_sha256": sha("middle")}],
        "trace": {"recalled_ids": ["a", "b"], "refined_ids": ["a", "b"],
                  "judge_pool_before_fit": ["a", "b"], "scored": [["a", .5, "relevant"], ["b", .4, "relevant"]]}}
    result = restore({"group": "x", "excluded": [], "id": "q"}, rec,
                     {"x": [e.model_dump(mode="json") for e in bank]})
    assert result["memories"]["a"]._hint == "strong"
    assert result["memories"]["a"]._hint_score == .9
    assert [e.id for e in result["analogies"]] == ["a"]


def test_scan_also_includes_analogy_beyond_prefix_without_duplicates():
    make = lambda eid: SimpleNamespace(id=eid)
    restored = {"pool": [make("a"), make("b"), make("c")], "analogies": [make("c"), make("a")]}
    assert scan_ids(restored, 1) == ["a", "c"]


@pytest.mark.asyncio
async def test_latency_trials_do_not_cache_identical_http_requests(tmp_path, monkeypatch):
    recorder = FreshHTTP(tmp_path, ["private-test-token"])
    post = AsyncMock(return_value=httpx.Response(200, json={"echo": "private-test-token"}))
    monkeypatch.setattr(recorder.client, "post", post)
    try:
        for _ in range(2):
            await recorder.post("https://example.invalid/systemone", json={"state": "same"})
    finally:
        await recorder.client.aclose()
    assert post.await_count == 2
    assert len(list(tmp_path.glob("http-*.json"))) == 2
    assert "private-test-token" not in (tmp_path/"http-000.json").read_text()


def test_failed_trial_does_not_count_a_fallback_as_successful_rescue(tmp_path):
    (tmp_path/"plan.json").write_text(json.dumps({"target_question_ids": ["q"], "control_question_ids": [],
        "target_pairs": [{"id": "q", "memory_id": "a", "group": "physics", "stage": "judge_budget_cut"}],
        "visibility_upper_bounds": {}}))
    row = {"id": "q", "arm": "scan12", "judge_ok": False, "jev_stage_s": 6., "wire_attempts": 4,
           "injected_ids": ["a"], "historical_injected_ids": [], "scan_ids": ["a"], "p": {}, "q": {}}
    (tmp_path/"results.jsonl").write_text(json.dumps(row)+"\n")
    result = summarize(tmp_path)["arms"]["scan12"]
    assert result["lost_pairs_injected"] == 0
    assert result["wire_attempts"] == 4
    assert result["latency_p50_s"] == 6.


@pytest.mark.asyncio
async def test_low_p_high_choice_candidate_keeps_its_comparison_opportunity(monkeypatch):
    for key,value in {"judge.scan_pool":6,"judge.scan_batch":3,"judge.compare_k":6,
                      "judge.prompt":"v1","exemplar.hint":False}.items():
        monkeypatch.setitem(settings._values,key,value)
    enzymes=[Enzyme(id=f"e{i}",patterns=[Pattern(value="x")],then=f"rule-{i}") for i in range(6)]
    probabilities={0:.33,1:.7,2:.6,3:.5,4:.4,5:.32}
    monkeypatch.setattr(tc,"fit_pool",lambda context,enzymes:enzymes[:4])
    async def fake(state,questions,config,timeout=0):
        answers={}
        target_key=None
        for key,question in questions.items():
            if question["type"]=="noul":
                i=int(question["instructions"]["rule"]["建议"].split("-")[-1])
                answers[key]={"noul":probabilities[i]}
                if i==0:target_key=key
        if "best" in questions:
            options=questions["best"]["criteria"]
            target_key=next((k for k,v in options.items() if isinstance(v,dict) and v.get("建议")=="rule-0"),None)
            chosen=target_key or next(k for k in options if k!="none")
            answers["best"]={"probabilities":{chosen:.95,"none":.05}}
        return {"answers":answers}
    monkeypatch.setattr(tc,"system_one",fake)
    single=await tc.judge_applicability("context",enzymes,TypeSafeConfig(api_key="offline"))
    batched=await tc.judge_two_stage("context",enzymes,TypeSafeConfig(api_key="offline"))
    assert single.q["e0"]==.95
    assert batched.p["e0"]==.33 and batched.q["e0"]==.95
    results=lambda:[pl.MatchResult(e,.5,"relevant") for e in enzymes]
    assert "e0" in [r.enzyme.id for r in pl._apply_jev_dosage(results(),single,6)]
    assert "e0" in [r.enzyme.id for r in pl._apply_jev_dosage(results(),batched,6)]


@pytest.mark.asyncio
async def test_two_stage_reuses_incumbent_pool_before_scanning_overflow(monkeypatch):
    for key, value in {"judge.two_stage": True, "judge.scan_reuse_fit": True,
                        "judge.scan_pool": 6, "judge.scan_batch": 3,
                        "judge.compare_k": 6, "judge.prompt": "v1",
                        "exemplar.hint": False}.items():
        monkeypatch.setitem(settings._values, key, value)
    enzymes = [Enzyme(id=f"e{i}", patterns=[Pattern(value="x")], then=f"rule-{i}")
               for i in range(6)]
    calls = []
    monkeypatch.setattr(tc, "fit_pool", lambda context, enzymes: enzymes[:4])

    async def fake(state, questions, config, timeout=0):
        calls.append(questions)
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {"noul": .4}
        if "best" in questions:
            options = questions["best"]["criteria"]
            choices = [key for key in options if key != "none"]
            answers["best"] = {"probabilities": {**{key: .8 / len(choices) for key in choices}, "none": .2}}
        return {"answers": answers}

    monkeypatch.setattr(tc, "system_one", fake)
    app = await tc.judge_two_stage("context", enzymes, TypeSafeConfig(api_key="offline"))
    assert app is not None
    assert set(app.p) == {f"e{i}" for i in range(6)}
    assert set(app.q) == {f"e{i}" for i in range(4)}
    assert app.calls == 3
    assert len(calls) == 3  # incumbent joint, overflow scan, overflow comparison


@pytest.mark.asyncio
async def test_adaptive_overflow_only_scans_when_first_pass_has_no_gate(monkeypatch):
    for key, value in {"judge.two_stage": True, "judge.scan_reuse_fit": True,
                        "judge.adaptive_overflow": True, "judge.overflow_pool": 6,
                        "judge.scan_pool": 3, "judge.scan_batch": 3,
                        "judge.compare_k": 3, "judge.prompt": "v1",
                        "exemplar.hint": False}.items():
        monkeypatch.setitem(settings._values, key, value)
    enzymes = [Enzyme(id=f"e{i}", patterns=[Pattern(value="x")], then=f"rule-{i}")
               for i in range(6)]
    monkeypatch.setattr(tc, "fit_pool", lambda context, enzymes: enzymes[:2])

    async def fake(state, questions, config, timeout=0):
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                i = int(question["instructions"]["rule"]["建议"].split("-")[-1])
                answers[key] = {"noul": .9 if i >= 3 else .1}
            elif question["type"] == "choice":
                options = question["criteria"]
                chosen = next((key for key, value in options.items()
                               if isinstance(value, dict) and value.get("建议") == "rule-3"), None)
                if chosen:
                    answers[key] = {"probabilities": {chosen: .9, "none": .1}}
                else:
                    answers[key] = {"probabilities": {"none": .9}}
        return {"answers": answers}

    monkeypatch.setattr(tc, "system_one", fake)
    app = await tc.judge_two_stage("context", enzymes, TypeSafeConfig(api_key="offline"))
    selected = pl._apply_jev_dosage([pl.MatchResult(e, .5, "relevant") for e in enzymes], app, 6)
    assert "e3" in [r.enzyme.id for r in selected]
    assert "e3" in app.p and app.supplement_q.get("e3", 0) >= .8
    assert app.calls >= 4  # first pass plus overflow scan and comparison
