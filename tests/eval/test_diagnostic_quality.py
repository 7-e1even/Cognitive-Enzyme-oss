from tests.eval.diagnostic.quality_review import recover


def test_recovery_keeps_complete_objects_without_guessing_truncated_tail():
    text = '[{"id":"a","completeness":"usable","solution_in_question":false}, {"id":"b",'
    assert set(recover(text, {"a", "b"})) == {"a"}


def test_conflicting_duplicate_audits_remain_uncertain():
    text = ('[{"id":"a","completeness":"usable","solution_in_question":false},'
            '{"id":"a","completeness":"incomplete","solution_in_question":false}]')
    assert recover(text, {"a"})["a"]["completeness"] == "uncertain"


def test_foreign_ids_and_wrong_types_are_not_recovered():
    text = ('[{"id":"foreign","completeness":"usable","solution_in_question":false},'
            '{"id":"a","completeness":"usable","solution_in_question":"false"}]')
    assert recover(text, {"a"}) == {}
