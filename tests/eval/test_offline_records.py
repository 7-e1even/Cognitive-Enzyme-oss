import json
import socket

import pytest

from tests.eval.offline import records


def test_missing_recall_pool_is_unknown_but_recorded_empty_pool_is_zero():
    row = {"query_id": "q", "kind": "positive", "expected": ["m"], "injected": [],
           "attribution": {"m": "not_recalled"}}
    result = records.retrieval_summary([row])
    assert result["final_recall"] == 0
    assert result["first_stage_recall"] is None
    assert result["stage_evidence"] == {"saved_attribution": 1}
    result = records.retrieval_summary([{**row, "recalled_ids": []}])
    assert result["first_stage_recall"] == 0
    assert result["first_stage_label_coverage"] == 1


def test_offline_replay_uses_rescued_pool_and_excludes_service_failures():
    row = {"query_id": "q", "kind": "positive", "expected": ["m"], "injected": [],
           "attribution": {"m": "rerank_dropped"}, "jev_status": "ok", "jev": [["m", .2, .1]]}
    result = records.retrieval_summary([row, {**row, "error": "rate limit"},
                                        {**row, "rerank_failed": True}])
    assert result["expected_memories"] == 1
    assert result["stages"] == {"jev_cut": 1}
    assert result["service_errors"] == result["degraded"] == 1
    assert records.retrieval_summary([])["final_recall"] is None


def test_freeze_preserves_original_bytes_and_detects_tampering_offline(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    original = b'{"id":"q","injected":[]}\n'
    source.write_bytes(original)
    monkeypatch.setattr(records, "ROOT", tmp_path)
    monkeypatch.setattr(records, "source_files", lambda: {"hardbench/math/history/math.self.jsonl": source})
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("offline replay opened a socket"))
    destination = tmp_path / "saved"
    records.freeze(destination)
    source.write_text("new history")
    assert records.verify(destination)["files"]["hardbench/math/history/math.self.jsonl"]["sha256"] == records.digest(original)
    report = records.summarize(destination, {"math"})
    assert report["hardbench"][0]["first_stage_recall"] is None
    with pytest.raises(ValueError, match="不覆盖"):
        records.freeze(destination)
    (destination / "hardbench/math/history/math.self.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="已修改"):
        records.summarize(destination, {"math"})


def test_replay_keeps_different_fingerprints_separate(tmp_path, monkeypatch):
    source = tmp_path / "source.jsonl"
    base = {"query_id": "q", "kind": "positive", "domain": "math", "expected": ["m"], "injected": []}
    source.write_text("\n".join(json.dumps(row) for row in [
        {**base, "config_fp": "a", "injected": ["m"]},
        {**base, "config_fp": "a"},  # latest record replaces an earlier one
        {**base, "config_fp": "b", "injected": ["m"]}]))
    monkeypatch.setattr(records, "ROOT", tmp_path)
    monkeypatch.setattr(records, "source_files", lambda: {"recall/history/test.outcomes.jsonl": source})
    records.freeze(tmp_path / "saved")
    report = records.summarize(tmp_path / "saved", {"math"})
    assert [(r["config_fp"], r["final_recall"]) for r in report["labelled_retrieval"]] == [("a", 0), ("b", 1)]
