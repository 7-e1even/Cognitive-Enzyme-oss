"""评测语料/查询的 schema 校验。CLI: python -m tests.eval.validate"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from app.core import Enzyme

EVAL_DIR = Path(__file__).parent
# 可用 env 切换到别的基准(如 tests/eval/realbench 真实数据基准),评测链路整体复用
CORPUS_DIR = Path(os.environ.get("CE_EVAL_CORPUS_DIR", EVAL_DIR / "corpus"))
QUERIES_PATH = Path(os.environ.get("CE_EVAL_QUERIES", EVAL_DIR / "queries.json"))

REQUIRED_KEYS = ("id", "patterns", "then", "because", "domain",
                 "severity", "confidence", "tags", "origin")


def load_corpus(corpus_dir: Path) -> list[Enzyme]:
    enzymes: list[Enzyme] = []
    seen: set[str] = set()
    for f in sorted(corpus_dir.glob("*.json")):
        for i, raw in enumerate(json.loads(f.read_text(encoding="utf-8"))):
            # Validate raw dict before pydantic (pydantic defaults would mask missing keys)
            missing = [k for k in REQUIRED_KEYS if k not in raw]
            if missing:
                raise ValueError(f"{f.name}[{i}] (id={raw.get('id')}): 缺字段 {missing}")
            if raw["origin"] != "harvested":
                raise ValueError(f"{f.name}[{i}] (id={raw['id']}): origin 必须是 harvested,得到 {raw['origin']!r}")

            try:
                e = Enzyme.model_validate(raw)
            except ValidationError as exc:
                raise ValueError(f"{f.name}[{i}] (id={raw.get('id')}): {exc}") from exc
            if not e.patterns:
                raise ValueError(f"{f.name}[{i}] (id={e.id}): patterns 为空")
            if e.id in seen:
                raise ValueError(f"{f.name}[{i}]: 重复 id {e.id}")
            seen.add(e.id)
            enzymes.append(e)
    return enzymes


def load_queries(path: Path, valid_ids: set[str]) -> list[dict]:
    queries = json.loads(path.read_text(encoding="utf-8"))["queries"]
    qids: set[str] = set()
    for q in queries:
        for key in ("id", "kind", "domain", "text", "expected_ids", "acceptable_ids"):
            if key not in q:
                raise ValueError(f"query {q.get('id')} 缺字段 {key}")
        if q["id"] in qids:
            raise ValueError(f"重复 query id {q['id']}")
        qids.add(q["id"])
        if q["kind"] not in ("positive", "silent"):
            raise ValueError(f"query {q['id']}: 非法 kind {q['kind']}")
        for eid in q["expected_ids"] + q["acceptable_ids"]:
            if eid not in valid_ids:
                raise ValueError(f"query {q['id']} 引用不存在的酶 {eid}")
        if q["kind"] == "positive" and not q["expected_ids"]:
            raise ValueError(f"positive query {q['id']} 无 expected_ids")
        if q["kind"] == "silent" and q["expected_ids"]:
            raise ValueError(f"silent query {q['id']} 不应有 expected_ids")
    return queries


def main() -> int:
    enzymes = load_corpus(CORPUS_DIR)
    by_domain = Counter(e.domain for e in enzymes)
    print(f"语料: {len(enzymes)} 条  " +
          "  ".join(f"{d}:{n}" for d, n in sorted(by_domain.items())))
    if QUERIES_PATH.exists():
        queries = load_queries(QUERIES_PATH, {e.id for e in enzymes})
        kinds = Counter(q["kind"] for q in queries)
        print(f"查询: {len(queries)} 条  positive:{kinds['positive']} silent:{kinds['silent']}")
    else:
        print("queries.json 尚未生成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
