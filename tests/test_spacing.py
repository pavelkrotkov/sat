from datetime import datetime

from satprep.training.spacing import INTERVALS_BY_OUTCOME, next_due, update_after_attempt

NOW = datetime(2026, 8, 1, 12, 0)


def test_confident_wrong_is_soonest():
    intervals = {}
    for (correct, conf), (_, _floor) in INTERVALS_BY_OUTCOME.items():
        iv, _due = next_due(5.0, correct, conf, NOW)
        intervals[(correct, conf)] = iv
    assert intervals[(False, 3)] == min(intervals.values())
    assert intervals[(True, 3)] == max(intervals.values())


def test_correct_after_wrong_grows_modestly_then_shaky_repeats_earlier():
    grow, _ = next_due(5.0, True, 2, NOW)
    shaky, _ = next_due(5.0, True, 1, NOW)
    assert grow > shaky


def test_retirement_cap():
    iv, _ = next_due(500.0, True, 3, NOW)
    assert iv <= 140


def test_update_persists_state(db):
    conn, _path = db
    from conftest import add_question

    qid = add_question(conn)
    update_after_attempt(conn, qid, correct=0, confidence=3, now=NOW)
    row = conn.execute("SELECT * FROM question_state WHERE question_id=?", (qid,)).fetchone()
    assert row["times_wrong"] == 1 and row["confident_wrong_streak"] == 1
    assert row["due_at"].startswith("2026-08-0")
