import json

from satprep.corpus.ingest import ingest_bluebook
from satprep.db import connect, db_context


def _ingest(db_file):
    with db_context(db_file) as conn:
        return ingest_bluebook(conn)


def _write_corpus(tmp_path, records):
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    (out / "wrong_questions.json").write_text(json.dumps(records))
    return out


def _rec(
    uid,
    test="SAT Practice Test 4",
    num=1,
    module="Module 1",
    status="Incorrect",
    my="B; Incorrect",
    key="A",
):
    # distinct passage text per uid so fingerprints differ unless a test
    # deliberately duplicates content
    return {
        "uid": uid,
        "scraped_at": "2026-03-01T00:00:00+00:00",
        "test_name": test,
        "test_number": "4",
        "section": "Reading and Writing",
        "subject_bucket": "Reading and Writing",
        "module": module,
        "question_number": str(num),
        "domain": "",
        "skill": "",
        "my_answer": my,
        "correct_answer": key,
        "answer_status": status,
        "question_text": f"Passage text for {uid} here.",
        "answer_choices": [],
        "explanation": "Choice A is the best answer.",
        "images": [],
        "html_snapshot_path": "",
    }


def test_ingest_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    dbp = str(tmp_path / "t.db")
    _write_corpus(tmp_path, [_rec("u1"), _rec("u2", num=2)])
    s1 = _ingest(dbp)
    s2 = _ingest(dbp)
    assert s1["questions_added"] == 2
    assert s2["questions_added"] == 0 and s2["attempts_added"] == 0
    conn = connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
    conn.close()


def test_math_records_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    rec = _rec("m1")
    rec["subject_bucket"] = "Math"
    _write_corpus(tmp_path, [rec])
    stats = _ingest(str(tmp_path / "t.db"))
    assert stats["rw_records"] == 0 and stats["questions_added"] == 0


def test_duplicate_content_dedupes_by_fingerprint(tmp_path, monkeypatch):

    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    r1 = _rec("a1")
    r2 = _rec("a2", num=2)
    r2["question_text"] = r1["question_text"]
    _write_corpus(tmp_path, [r1, r2])
    _ingest(str(tmp_path / "t.db"))
    conn = connect(str(tmp_path / "t.db"))
    n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    occ = conn.execute("SELECT COUNT(*) FROM bluebook_occurrences").fetchone()[0]
    conn.close()
    # identical passage+empty stem+no choices -> same fingerprint -> deduped
    assert n == 1
    # but BOTH source occurrences are preserved with their placement identity
    assert occ == 2


def test_identical_content_occurrences_keep_placement(tmp_path, monkeypatch):
    """Issue #49: same content at different placements must not erase
    the source occurrence's test/module/question identity."""
    monkeypatch.setattr("satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json")
    r1 = _rec("a1")
    r2 = _rec("a2", num=2)
    r2["question_text"] = r1["question_text"]
    r2["test_name"] = "SAT Practice Test 5"
    _write_corpus(tmp_path, [r1, r2])
    _ingest(str(tmp_path / "t.db"))
    conn = connect(str(tmp_path / "t.db"))
    rows = conn.execute(
        "SELECT test_name, module, question_number, bluebook_uid FROM bluebook_occurrences ORDER BY test_name"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert {(r["test_name"], r["question_number"]) for r in rows} == {
        ("SAT Practice Test 4", "1"),
        ("SAT Practice Test 5", "2"),
    }


def test_unknown_correctness_never_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    rec = _rec("u9")
    rec["answer_status"] = ""
    rec["my_answer"] = ""
    _write_corpus(tmp_path, [rec])
    stats = _ingest(str(tmp_path / "t2.db"))
    assert stats["attempts_added"] == 0
