import pytest

from satprep.sessions import complete_session, create_session, review_payload, submit_answer
from conftest import add_question


def _setup(db):
    conn, path = db
    # historical errors + rights + fresh pool
    wq = []
    for i in range(5):
        qid = add_question(conn, passage=f"wp{i}", stem=f"ws{i}?",
                           choices=[f"a{i}", f"b{i}", f"c{i}", f"d{i}"], correct="A",
                           source="bluebook_test", pool="historical",
                           tags=("qualifier_strength",))
        conn.execute("INSERT INTO attempts (session_id, question_id, chosen_letter, correct, confidence, time_ms, mode, attempted_at) VALUES (?,?,?,?,0,0,'historical','2026-03-01')",
                     (f"sw{i}", qid, "C", 0))
        wq.append(qid)
    for i in range(8):
        add_question(conn, passage=f"rp{i}", stem=f"rs{i}?",
                     choices=["ra", "rb", "rc", "rd"], correct="B",
                     source="bluebook_test", pool="historical", tags=("qualifier_strength",))
    for i in range(10):
        add_question(conn, passage=f"fp{i}", stem=f"fs{i}?",
                     choices=["fa", "fb", "fc", "fd"], correct="D",
                     source="college_board_question_bank", pool="fresh_training",
                     difficulty="hard", tags=("qualifier_strength",))
    conn.commit()
    return path


def test_full_lifecycle_records_scores_and_review(db):
    path = _setup(db)
    sess = create_session("targeted_drill", count=6, seed="life", db_path=path)
    sid = sess["plan"]["session_id"]
    answers = {}
    for idx, q in enumerate(sess["questions"]):
        letter = "A" if idx % 2 == 0 else q["choices"][1]["letter"]  # mix of key/wrong guesses
        conf = 3 if idx % 2 == 0 else 1
        res = submit_answer(sid, q["id"], letter, conf, 42000, db_path=path)
        answers[q["id"]] = res
    summary = complete_session(sid, db_path=path)
    assert summary["total"] == len(sess["questions"])
    assert summary["correct"] == sum(r["correct"] for r in answers.values())
    reviews = review_payload(sid, db_path=path)
    for r in reviews:
        if not r["correct"]:
            assert r["key_letter"] and r["trap_tags"] is not None


def test_benchmark_answer_marks_seen(db):
    from satprep.db import connect

    path = _setup(db)
    sess = create_session("fresh_benchmark", count=4, seed="b", db_path=path)
    if not sess["questions"]:
        pytest.skip("no protected items in split")
    sid = sess["plan"]["session_id"]
    q = sess["questions"][0]
    submit_answer(sid, q["id"], "Z", 2, 1000, db_path=path)
    conn = connect(path)
    row = conn.execute("SELECT pool, seen_benchmark FROM questions WHERE id=?",
                       (q["id"],)).fetchone()
    conn.close()
    assert row["seen_benchmark"] == 1 and row["pool"] != "protected_benchmark"


def test_confidence_clamped(db):
    path = _setup(db)
    sess = create_session("error_clinic", count=3, seed="c", db_path=path)
    q = sess["questions"][0]
    res = submit_answer(sid := sess["plan"]["session_id"], q["id"],
                        sess["questions"][0]["choices"][0]["letter"], 9, 5, db_path=path)
    from satprep.db import connect
    conn = connect(path)
    row = conn.execute("SELECT MAX(confidence) FROM attempts WHERE session_id=?", (sid,)).fetchone()[0]
    conn.close()
    assert row <= 3
