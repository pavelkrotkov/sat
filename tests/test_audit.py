"""Issue #49: corpus audit regression tests."""

import json

from satprep.corpus.audit import _has_determinable_correctness, audit_bluebook, audit_passes
from tests.conftest import add_question


def _seed_sources(tmp_path, monkeypatch, records):
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    (out / "wrong_questions.json").write_text(json.dumps(records))
    monkeypatch.setattr("satprep.config.BLUEBOOK_JSON", out / "wrong_questions.json")


def _rec(uid, *, test="SAT Practice Test 4", module="Module 1", num="1", status="Correct"):
    return {
        "uid": uid,
        "test_name": test,
        "subject_bucket": "Reading and Writing",
        "module": module,
        "question_number": str(num),
        "answer_status": status,
        "my_answer": "A; Correct",
        "correct_answer": "A",
        "scraped_at": "2026-01-01",
    }


def _seed_occurrences(conn, records, qid_for=None):
    """Insert an occurrence per record, resolving to a question row, plus the
    matching historical attempt (per-occurrence preservation gate T3)."""
    for rec in records:
        qid = (
            qid_for(rec)
            if qid_for
            else add_question(
                conn,
                passage=f"P{rec['uid']}",
                stem=f"Q{rec['uid']}?",
                choices=("a", "b", "c", "d"),
                correct="A",
                source="bluebook_test",
                pool="historical",
            )
        )
        conn.execute(
            """INSERT INTO bluebook_occurrences
                 (bluebook_uid, test_name, module, question_number, subject,
                  fingerprint, question_id, answer_status, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                rec["uid"],
                rec["test_name"],
                rec["module"],
                rec["question_number"],
                "Reading and Writing",
                f"fp{qid}",
                qid,
                rec["answer_status"],
                rec["scraped_at"],
            ),
        )
        if _has_determinable_correctness(rec):
            conn.execute(
                """INSERT INTO attempts (session_id, question_id, chosen_letter,
                                          correct, confidence, time_ms, mode, attempted_at)
                   VALUES (?,?,?,?,0,0,'historical',?)""",
                (f"hist:{rec['uid']}", qid, "A", 1, rec["scraped_at"]),
            )


def test_audit_counts_source_occurrences(db, tmp_path, monkeypatch):
    conn, _ = db
    recs = [
        _rec("u1"),
        _rec("u2", num="2"),
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
    qid = add_question(
        conn,
        passage="P",
        stem="Q?",
        choices=(),
        correct="A",
        source="bluebook_test",
        pool="historical",
    )
    conn.execute(
        """INSERT INTO bluebook_occurrences
             (bluebook_uid, test_name, module, question_number, subject,
              fingerprint, question_id, answer_status, scraped_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            recs[0]["uid"],
            recs[0]["test_name"],
            recs[0]["module"],
            recs[0]["question_number"],
            "Reading and Writing",
            "fp",
            qid,
            recs[0]["answer_status"],
            recs[0]["scraped_at"],
        ),
    )
    report = audit_bluebook(conn)
    assert any("choices" in g["missing"] for g in report["question_gaps"])
    assert not audit_passes(report)


def test_audit_reports_attempts_preserved(db, tmp_path, monkeypatch):
    """T3: the audit counts historical attempts and flags a missing one."""
    conn, _ = db
    recs = [_rec("u1")]
    _seed_sources(tmp_path, monkeypatch, recs)
    _seed_occurrences(conn, recs)
    report = audit_bluebook(conn)
    assert report["attempts_preserved"] == 1
    assert report["attempts_missing"] == [] and audit_passes(report)
    # delete the attempt -> the audit now flags it as missing
    conn.execute("DELETE FROM attempts WHERE mode='historical'")
    report = audit_bluebook(conn)
    assert report["attempts_preserved"] == 0
    assert report["attempts_missing"] == ["u1"]
    assert not audit_passes(report)


def test_audit_rejects_missing_source(db, tmp_path, monkeypatch):
    """T5: a missing source scrape must fail the audit, never report PASS."""
    conn, _ = db
    # Do not write outputs/wrong_questions.json at all.
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    report = audit_bluebook(conn)
    assert not audit_passes(report)
    assert report["failures"]


def test_audit_rejects_zero_source_records(db, tmp_path, monkeypatch):
    """T5: an empty R&W source (zero records) must not read as a clean PASS."""
    conn, _ = db
    _seed_sources(tmp_path, monkeypatch, [])
    report = audit_bluebook(conn)
    assert not audit_passes(report)
    assert any("zero R&W" in f for f in report["failures"])
