"""Issue #49: corpus audit regression tests."""

import json

import pytest

from satprep.db import connect, db_context
from satprep.corpus.audit import audit_bluebook, audit_passes
from satprep.corpus.repair import repair_bluebook
from tests.conftest import add_question  # noqa: F401  (fixture helper)


def _seed_sources(tmp_path, monkeypatch, records):
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    (out / "wrong_questions.json").write_text(json.dumps(records))
    monkeypatch.setattr("satprep.config.BLUEBOOK_JSON", out / "wrong_questions.json")


def _rec(uid, *, test="SAT Practice Test 4", module="Module 1", num="1",
         status="Correct"):
    return {
        "uid": uid, "test_name": test, "subject_bucket": "Reading and Writing",
        "module": module, "question_number": str(num), "answer_status": status,
        "my_answer": "A; Correct", "correct_answer": "A", "scraped_at": "2026-01-01",
    }


def _seed_occurrences(conn, records, qid_for=None):
    """Insert an occurrence per record, resolving to a question row."""
    for rec in records:
        qid = qid_for(rec) if qid_for else add_question(
            conn, passage=f"P{rec['uid']}", stem=f"Q{rec['uid']}?",
            choices=("a", "b", "c", "d"), correct="A",
            source="bluebook_test", pool="historical")
        conn.execute(
            """INSERT INTO bluebook_occurrences
                 (bluebook_uid, test_name, module, question_number, subject,
                  fingerprint, question_id, answer_status, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rec["uid"], rec["test_name"], rec["module"], rec["question_number"],
             "Reading and Writing", f"fp{qid}", qid, rec["answer_status"],
             rec["scraped_at"]),
        )


def test_audit_counts_source_occurrences(db, tmp_path, monkeypatch):
    conn, _ = db
    recs = [
        _rec("u1"), _rec("u2", num="2"),
        _rec("u3", module="Module 2", num="7"),
        # duplicate placement: same test/module/qn as u1
        _rec("u1-dup", num="1", status="Incorrect"),
    ]
    _seed_sources(tmp_path, monkeypatch, recs)
    _seed_occurrences(conn, recs)
    report = audit_bluebook(conn)
    assert report["source_records"] == 4
    assert report["occurrences_total"] == 4
    assert report["occurrences_with_question"] == 4
    assert report["missing_occurrences"] == []
    assert len(report["duplicate_placements"]) == 1
    assert report["duplicate_placements"][0]["count"] == 2
    assert report["failures"] == []


def test_audit_flags_missing_occurrence(db, tmp_path, monkeypatch):
    conn, _ = db
    recs = [_rec("u1"), _rec("u2", num="2")]
    _seed_sources(tmp_path, monkeypatch, recs)
    # only u1 gets an occurrence row
    _seed_occurrences(conn, [recs[0]])
    report = audit_bluebook(conn)
    assert len(report["missing_occurrences"]) == 1
    assert report["missing_occurrences"][0]["bluebook_uid"] == "u2"
    assert not audit_passes(report)


def test_audit_flags_missing_fields(db, tmp_path, monkeypatch):
    conn, _ = db
    recs = [_rec("u1")]
    _seed_sources(tmp_path, monkeypatch, recs)
    # question with no choices is a gap
    qid = add_question(conn, passage="P", stem="Q?", choices=(), correct="A",
                       source="bluebook_test", pool="historical")
    conn.execute(
        """INSERT INTO bluebook_occurrences
             (bluebook_uid, test_name, module, question_number, subject,
              fingerprint, question_id, answer_status, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (recs[0]["uid"], recs[0]["test_name"], recs[0]["module"],
         recs[0]["question_number"], "Reading and Writing", "fp", qid,
         recs[0]["answer_status"], recs[0]["scraped_at"]),
    )
    report = audit_bluebook(conn)
    assert any("choices" in g["missing"] for g in report["question_gaps"])
    assert not audit_passes(report)


def test_audit_reports_attempts_preserved(db, tmp_path, monkeypatch):
    conn, _ = db
    recs = [_rec("u1")]
    _seed_sources(tmp_path, monkeypatch, recs)
    _seed_occurrences(conn, recs)
    report = audit_bluebook(conn)
    assert report["attempts_preserved"] == 0
    # with an attempt
    from tests.conftest import add_attempt
    qid = conn.execute("SELECT question_id FROM bluebook_occurrences").fetchone()[0]
    add_attempt(conn, qid, correct=1, mode="historical")
    report = audit_bluebook(conn)
    assert report["attempts_preserved"] == 1
