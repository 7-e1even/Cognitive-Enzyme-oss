"""Shared grading contract: an unavailable judge is not a wrong answer.

Keep solver failures, judge failures and genuine wrong answers separate.  This
also reads historical rows written before ``correct=None`` was introduced.
"""
from __future__ import annotations


def outcome(row: dict | None) -> bool | None:
    if not row or row.get("error") or row.get("judge_ok") is False:
        return None
    if row.get("grade_status") in ("ungraded", "error"):
        return None
    value = row.get("correct")
    return value if isinstance(value, bool) else None


def judge_grade(verdict: bool | None, response: dict, *, model: str = "") -> dict:
    result = {
        "correct": verdict,
        "graded_by": "judge",
        "judge_ok": verdict is not None,
        "grade_status": "graded" if verdict is not None else "ungraded",
        "judge_model": model,
        "judge_text": (response.get("text") or "")[-1000:],
        "judge_secs": response.get("secs"),
        "judge_usage": response.get("usage"),
        "grading_error": None,
    }
    if verdict is None:
        result["grading_error"] = response.get("error") or "unparseable judge verdict"
    return result


def failure_kind(row: dict | None) -> str:
    if row is None:
        return "missing"
    if row.get("error"):
        return "solver_error"
    return "ungraded" if outcome(row) is None else "graded"
