import json
import pytest
from tests.eval.validate import load_corpus, load_queries

GOOD_ENZYME = {
    "id": "math-001", "patterns": [{"value": "浮点"}], "then": "浮点比较用容差",
    "because": "二进制精度", "domain": "math", "severity": "info",
    "confidence": 0.55, "tags": [], "origin": "harvested",
}


def test_load_corpus_ok(tmp_path):
    (tmp_path / "math.json").write_text(json.dumps([GOOD_ENZYME]), encoding="utf-8")
    enzymes = load_corpus(tmp_path)
    assert len(enzymes) == 1 and enzymes[0].id == "math-001"


def test_load_corpus_duplicate_id(tmp_path):
    (tmp_path / "math.json").write_text(
        json.dumps([GOOD_ENZYME, GOOD_ENZYME]), encoding="utf-8")
    with pytest.raises(ValueError, match="math-001"):
        load_corpus(tmp_path)


def test_load_corpus_bad_schema(tmp_path):
    bad = {**GOOD_ENZYME, "patterns": []}  # patterns 不能为空
    del bad["id"]
    (tmp_path / "math.json").write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_corpus(tmp_path)


def test_load_corpus_missing_required_key(tmp_path):
    bad = {k: v for k, v in GOOD_ENZYME.items() if k != "id"}
    (tmp_path / "math.json").write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError, match="缺字段"):
        load_corpus(tmp_path)


def test_load_corpus_wrong_origin(tmp_path):
    bad = {**GOOD_ENZYME, "origin": "manual"}
    (tmp_path / "math.json").write_text(json.dumps([bad]), encoding="utf-8")
    with pytest.raises(ValueError, match="origin"):
        load_corpus(tmp_path)


def test_load_queries_unknown_id(tmp_path):
    q = {"queries": [{"id": "q1", "kind": "positive", "domain": "math",
                      "text": "...", "expected_ids": ["nope"], "acceptable_ids": []}]}
    p = tmp_path / "queries.json"
    p.write_text(json.dumps(q), encoding="utf-8")
    with pytest.raises(ValueError, match="nope"):
        load_queries(p, valid_ids={"math-001"})
