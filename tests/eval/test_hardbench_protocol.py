"""Offline regression tests for missing grades, paired scoring and run identity."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.eval.hardbench import retest
from tests.eval.hardbench.grading import failure_kind, judge_grade, outcome
from tests.eval.hardbench.run import extract_final, grade_local, parse_verdict
from tests.eval.realbench.uplift_eval import parse_opencode
from tests.eval.realbench import uplift_eval
from tests.eval.opencode_runner import CLOSED_BOOK, parse_messages


def test_failed_judge_is_unknown_including_legacy_false_rows():
    assert outcome({"correct": False, "judge_ok": False}) is None
    assert outcome({"correct": False, "judge_ok": True}) is False
    assert outcome({"correct": True, "error": "timeout"}) is None
    assert outcome({"correct": "false"}) is None
    assert outcome({"correct": None}) is None
    assert outcome(None) is None
    assert failure_kind({"correct": False, "judge_ok": False}) == "ungraded"
    assert judge_grade(None, {"error": "timeout"})["correct"] is None
    assert judge_grade(False, {"text": "NOT_EQUIVALENT"})["grade_status"] == "graded"


def test_verdict_requires_an_explicit_final_label():
    assert parse_verdict("Explanation\n**EQUIVALENT**") is True
    assert parse_verdict("NOT_EQUIVALENT") is False
    assert parse_verdict("I cannot decide whether these are EQUIVALENT") is None
    assert parse_verdict("EQUIVALENT\nActually I am uncertain") is None


def test_judge_receives_the_complete_multipart_final_answer():
    answer = "(1) " + "x + " * 300 + "y; (2) z = 17"
    task = {"kind": "free"}
    assert grade_local(task, "Answer: " + answer)["pred"] == answer
    multiline = "(1) x = 2\n\n(2) " + "y + " * 300 + "z = 17"
    assert extract_final("Explanation\nFINAL ANSWER\n" + multiline) == multiline


def test_opencode_closed_book_events_and_tool_rejection():
    events = [{"type": "text", "part": {"text": "Answer: 2"}},
              {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 4, "output": 3}}}]
    encode = lambda xs: "\n".join(json.dumps(x) for x in xs)
    assert parse_opencode(encode(events))["ok"]
    assert parse_opencode(encode(events))["usage"]["output"] == 3
    assert not parse_opencode(encode([*events, {"type": "tool_use", "part": {}}]))["ok"]
    assert not parse_opencode(encode(events[:1]))["ok"]
    assert parse_opencode(encode([{"type": "step_finish", "part": {"reason": "length"}}]))["ok"]


def test_opencode_v2_reuses_desktop_login_without_loading_repository(tmp_path, monkeypatch):
    executable = "/desktop/opencode-cli"
    monkeypatch.setitem(uplift_eval._OPENCODE_VERSIONS, executable, "opencode v2.0.29")
    calls = []
    delivered = []

    class Process:
        returncode = 0

        def __init__(self, command):
            self.command = command

        async def communicate(self, data=None):
            if "api" in self.command:
                value = ({"id": "ses_test", "outcome": "succeeded", "cost": 0}
                         if "session.get" in self.command else [{"type": "user", "text": delivered[0]}, {"type": "assistant", "finish": "stop",
                             "model": {"id": "deepseek-v4.1-flash-free", "providerID": "opencode", "variant": "low"},
                             "content": [{"type": "text", "text": "Answer: 2"}]}])
                return json.dumps({"data": value}).encode(), b""
            delivered.append(data.decode())
            events = [{"type": "text", "sessionID": "ses_test", "part": {"text": "Answer: 2"}}]
            return "\n".join(json.dumps(e) for e in events).encode(), b""

    async def start(*command, **options):
        calls.append((command, options))
        return Process(command)

    monkeypatch.setattr(uplift_eval.asyncio, "create_subprocess_exec", start)
    args = SimpleNamespace(opencode_bin=executable, model="opencode/deepseek-v4.1-flash-free",
                           max_output_tokens=1024, timeout=10)
    result = asyncio.run(uplift_eval.ask_opencode("Compute", args,
        {"XDG_DATA_HOME": "/desktop/login-data"}, tmp_path))
    command, options = calls[0]
    assert result["ok"]
    assert result["text"] == "Answer: 2"
    assert options["env"]["XDG_DATA_HOME"] == "/desktop/login-data"
    assert str(tmp_path) not in options["cwd"]
    config = json.loads(options["env"]["OPENCODE_CONFIG_CONTENT"])
    assert config["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert "system" not in config["agents"]["build"]
    assert "providers" not in config  # use the desktop's live model definition
    assert "--standalone" in command
    assert "--auto" not in command
    assert options["stdin"] == asyncio.subprocess.PIPE
    assert delivered == [CLOSED_BOOK + "Compute"]
    assert "Compute" not in command
    assert result["input_delivery_verified"]
    assert "opencode/deepseek-v4.1-flash-free#low" in command


def test_opencode_v2_checks_tools_model_cost_and_real_finish():
    model = "opencode/deepseek-v4.1-flash-free#high"
    info = {"id": "s", "outcome": "succeeded", "cost": 0}
    row = {"type": "assistant", "finish": "stop", "content": [{"type": "text", "text": "Answer: 2"}],
           "model": {"providerID": "opencode", "id": "deepseek-v4.1-flash-free", "variant": "high"}}
    assert parse_messages(info, [row], model)["ok"]
    assert not parse_messages(info, [{**row, "content": [{"type": "tool"}]}], model)["ok"]
    assert not parse_messages({**info, "cost": .001}, [row], model)["ok"]
    assert not parse_messages(info, [{**row, "model": {**row["model"], "id": "paid-model"}}], model)["ok"]
    assert not parse_messages(info, [{**row, "model": {**row["model"], "variant": "low"}}], model)["ok"]
    assert parse_messages(info, [{**row, "finish": "length", "content": []}], model)["ok"]


def test_forbidden_tool_attempt_is_wrong_without_resampling(tmp_path, monkeypatch):
    calls = []
    async def invalid(*unused):
        calls.append(True)
        return {"ok": False, "error": "tool attempt", "protocol_error": "tool_attempt"}
    monkeypatch.setattr(uplift_eval, "ask_opencode", invalid)
    args = SimpleNamespace(runner="opencode", max_wait=0)
    result = asyncio.run(uplift_eval.ask_with_retry("Compute", args, {}, tmp_path, lambda x: None))
    assert len(calls) == 1
    assert result["ok"] and result["stop"] == "protocol_violation"
    assert grade_local({"kind": "free"}, result["text"])["correct"] is False


def test_solver_time_budget_is_not_retried_or_confused_with_judge_timeout(tmp_path, monkeypatch):
    calls = []
    async def timeout(*unused):
        calls.append(True)
        return {"ok": False, "error": "timeout 600s", "timed_out": True}
    monkeypatch.setattr(uplift_eval, "ask_opencode", timeout)
    args = SimpleNamespace(runner="opencode", max_wait=60, purpose="solve", service_retries=0)
    result = asyncio.run(uplift_eval.ask_with_retry("Compute", args, {}, tmp_path, lambda x: None))
    assert len(calls) == 1
    assert result["ok"] and result["budget_exhausted"] and result["stop"] == "time_budget"
    assert grade_local({"kind": "free"}, result["text"])["correct"] is False
    calls.clear()
    args.purpose = "grade"
    result = asyncio.run(uplift_eval.ask_with_retry("Compare", args, {}, tmp_path, lambda x: None))
    assert len(calls) == 1 and not result["ok"]
    assert judge_grade(None, result)["correct"] is None


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    monkeypatch.setattr(retest, "DATA", tmp_path)
    problems = [{"id": "q", "problem": "Compute", "domain": "physics", "kind": "free", "answer": "2"}]
    (tmp_path / "problems.json").write_text(json.dumps(problems))
    memories = [{"id": "m", "then": "Check units", "because": "", "confidence": .5, "severity": "info"}]
    primes = [{"id": "q", **{m: {"memories": memories, "jev_status": "ok", "judge": None}
                              for m in ("single", "two_stage")}}]
    monkeypatch.setattr(retest, "load_primes", lambda args: primes)
    args = SimpleNamespace(output_dir=str(tmp_path / "run"), run_name="test", lib="self_v3", mode="two_stage",
        runner="opencode", model="opencode/deepseek-v4.1-flash-free", judge_model="", thinking="high",
        max_output_tokens=1024, opencode_bin="unused", timeout=10, judge_timeout=10, grading_timeout=10, max_wait=0,
        concurrency=1, limit=0, key_from="", key_env="unused", provider_name="magpie", provider_config="unused")
    return args, primes


def test_manifest_prevents_mixing_models_and_frozen_memories(evaluation):
    args, primes = evaluation
    retest.ensure_manifest(args, primes)
    args.model = "another-model"
    with pytest.raises(SystemExit, match="禁止混合"):
        retest.ensure_manifest(args, primes)
    args.model = "opencode/deepseek-v4.1-flash-free"
    primes[0]["two_stage"]["memories"][0]["then"] = "Changed advice"
    with pytest.raises(SystemExit, match="禁止混合"):
        retest.ensure_manifest(args, primes)


def test_paired_report_excludes_failed_grades_and_keeps_coverage(evaluation):
    args, primes = evaluation
    retest.ensure_manifest(args, primes)
    path = retest.output_dir(args) / "solves.jsonl"
    rows = [{"id": "q", "arm": "ctl", "correct": False, "judge_ok": True},
            {"id": "q", "arm": "mem", "correct": False, "judge_ok": False}]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    text = retest.report(args)
    assert "| mem | 1 | 0 | 1 | 0 | 0 |" in text
    assert "有效配对" not in text


def test_grade_only_reuses_saved_answers_and_does_not_solve(evaluation, monkeypatch):
    args, primes = evaluation
    retest.ensure_manifest(args, primes)
    path = retest.output_dir(args) / "solves.jsonl"
    path.write_text(json.dumps({"id": "q", "arm": "mem", "mode": "two_stage", "injected": ["m"],
        "text": "Answer: x", "correct": False, "judge_ok": False}) + "\n")
    calls = []
    async def judge(prompt, *unused):
        assert "Official reference answer" in prompt
        calls.append(prompt)
        return {"ok": True, "text": "NOT_EQUIVALENT", "secs": .1}
    monkeypatch.setattr(retest, "ask_with_retry", judge)
    asyncio.run(retest.stage_solve(args, grade_only=True))
    rows = retest.load_solves(path)
    assert len(calls) == 1
    assert outcome(rows["mem", "q"]) is False
    assert ("ctl", "q") not in rows


def test_regrade_saved_preserves_source_and_does_not_keep_old_verdict(evaluation, monkeypatch, tmp_path):
    args, _ = evaluation
    source = tmp_path / "legacy.jsonl"
    original = {"id": "q", "arm": "mem", "injected": ["m"], "text": "Answer: x",
                "correct": False, "judge_ok": True, "grading_error": "old timeout"}
    source.write_text(json.dumps(original) + "\n")
    args.answers_from = str(source)
    async def judge(prompt, *unused):
        assert "Official reference answer" in prompt
        return {"ok": True, "text": "EQUIVALENT"}
    monkeypatch.setattr(retest, "ask_with_retry", judge)
    asyncio.run(retest.stage_solve(args, grade_only=True))
    saved = retest.load_solves(retest.output_dir(args) / "solves.jsonl")["mem", "q"]
    assert outcome(saved) is True
    assert saved["previous_grade"]["correct"] is False
    assert saved["grading_error"] is None
    assert json.loads(source.read_text()) == original
