"""探索槽(exploration slot)单测 — 纯内存,无需连库。

直接跑: python tests/test_exploration.py
覆盖:命中概率时追加假设酶并标记、不命中不追加、rate=0 关闭、
     max_results 硬顶、只收演化来源、confidence 门槛、不重复入选、
     formatter 透出 exploratory 标记。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.matching.pipeline as pipeline  # noqa: E402
from app.core import Enzyme, Origin, Pattern, Severity  # noqa: E402
from app.delivery.formatter import format_match_results_slim  # noqa: E402
from app.matching.pipeline import MatchResult, _apply_dosage  # noqa: E402

FIRE = lambda: 0.0    # noqa: E731  必命中(默认 rate 0.2 > 0.0)
MISS = lambda: 0.99   # noqa: E731  必不命中


def _mk(eid, origin=Origin.HARVESTED, conf=0.8, tier="relevant",
        score=0.5, severity=Severity.INFO):
    e = Enzyme(id=eid, patterns=[Pattern(value="x")], then=f"then-{eid}",
               severity=severity, confidence=conf, origin=origin)
    return MatchResult(enzyme=e, score=score, tier=tier)


def _five_regulars():
    return [_mk(f"r{i}") for i in range(5)]


def test_explorer_promoted():
    """落选的 dream 假设酶被探索槽追加,带 exploratory 标记、排在最后。"""
    dream = _mk("d1", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.1)
    dosed = _apply_dosage(_five_regulars() + [dream], max_results=15, rng=FIRE)
    assert len(dosed) == 6
    assert dosed[-1].enzyme.id == "d1" and dosed[-1].exploratory
    assert not any(r.exploratory for r in dosed[:-1])
    print("[ok] explorer_promoted")


def test_rng_miss_no_explorer():
    dream = _mk("d1", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.1)
    dosed = _apply_dosage(_five_regulars() + [dream], max_results=15, rng=MISS)
    assert len(dosed) == 5 and not any(r.exploratory for r in dosed)
    print("[ok] rng_miss_no_explorer")


def test_rate_zero_disables():
    saved = pipeline._EXPLORATION_RATE
    pipeline._EXPLORATION_RATE = 0.0
    try:
        dream = _mk("d1", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.1)
        dosed = _apply_dosage(_five_regulars() + [dream], max_results=15, rng=FIRE)
        assert len(dosed) == 5
    finally:
        pipeline._EXPLORATION_RATE = saved
    print("[ok] rate_zero_disables")


def test_max_results_ceiling():
    """剂量已到硬顶时,探索槽让位,绝不超发。"""
    dream = _mk("d1", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.1)
    dosed = _apply_dosage(_five_regulars() + [dream], max_results=5, rng=FIRE)
    assert len(dosed) == 5 and not any(r.exploratory for r in dosed)
    print("[ok] max_results_ceiling")


def test_eligibility_gates():
    """非演化来源、confidence≥0.6 的都不占探索名额。"""
    harvested = _mk("h1", origin=Origin.HARVESTED, conf=0.2, tier="peripheral", score=0.1)
    mature_mut = _mk("m1", origin=Origin.MUTATED, conf=0.7, tier="peripheral", score=0.1)
    dosed = _apply_dosage(_five_regulars() + [harvested, mature_mut],
                          max_results=15, rng=FIRE)
    assert len(dosed) == 5
    print("[ok] eligibility_gates")


def test_no_duplicate_when_already_dosed():
    """假设酶已凭分数入选时,不再重复占探索槽。"""
    mutant = _mk("m1", origin=Origin.MUTATED, conf=0.45)
    dosed = _apply_dosage([mutant] + [_mk(f"r{i}") for i in range(3)],
                          max_results=15, rng=FIRE)
    assert [r.enzyme.id for r in dosed].count("m1") == 1
    assert not any(r.exploratory for r in dosed)
    print("[ok] no_duplicate_when_already_dosed")


def test_picks_most_relevant_hypothesis():
    """多条落选假设中取排序最靠前(与情境最相关)的一条。"""
    d_hi = _mk("d_hi", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.3)
    d_lo = _mk("d_lo", origin=Origin.CROSSED, conf=0.5, tier="peripheral", score=0.1)
    dosed = _apply_dosage(_five_regulars() + [d_hi, d_lo], max_results=15, rng=FIRE)
    assert dosed[-1].enzyme.id == "d_hi" and dosed[-1].exploratory
    print("[ok] picks_most_relevant_hypothesis")


def test_formatter_marks_exploratory():
    regular = _mk("r1")
    dream = _mk("d1", origin=Origin.EVOLVED, conf=0.2, tier="peripheral", score=0.1)
    dream.exploratory = True
    out = format_match_results_slim([regular, dream])
    assert "exploratory" not in out[0]
    assert out[1]["exploratory"] is True
    print("[ok] formatter_marks_exploratory")


def test_formatter_exposes_methodology_scope():
    general = _mk("g1")
    general.enzyme.tags = ["scope:general"]
    specific = _mk("s1")
    out = format_match_results_slim([general, specific])
    assert out[0]["scope"] == "general"
    assert out[1]["scope"] == "specific"
    print("[ok] formatter_exposes_methodology_scope")


def main():
    print("=" * 50)
    print("探索槽单测")
    print("=" * 50)
    test_explorer_promoted()
    test_rng_miss_no_explorer()
    test_rate_zero_disables()
    test_max_results_ceiling()
    test_eligibility_gates()
    test_no_duplicate_when_already_dosed()
    test_picks_most_relevant_hypothesis()
    test_formatter_marks_exploratory()
    test_formatter_exposes_methodology_scope()
    print("=" * 50)
    print("All passed [PASS]")


if __name__ == "__main__":
    main()
