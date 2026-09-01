"""Tests for the auditable question-review persistence workflow (issue #37)."""
from __future__ import annotations

import json
import pathlib

import pytest

from satprep.reviews import (
    APPROVED,
    DRAFT,
    EDITED,
    REJECTED,
    DuplicateReviewError,
    ReviewStateError,
    delete_review,
    edit_review,
    ensure_review_schema,
    export_review,
    get_review,
    list_reviews,
    transition,
    upsert_draft,
)

# ---------------------------------------------------------------------------
# Fixture: a small in-memory DB with the questions table + one question.
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_test TEXT DEFAULT '',
        source_question_number TEXT DEFAULT '',
        module TEXT DEFAULT '',
        passage TEXT DEFAULT '',
        stem TEXT DEFAULT '',
        choices_json TEXT NOT NULL DEFAULT '[]',
        correct_letter TEXT NOT NULL,
        rationale TEXT DEFAULT '',
        images_json TEXT DEFAULT '[]',
        official_domain TEXT DEFAULT '',
        official_skill TEXT DEFAULT '',
        skill_source TEXT DEFAULT 'unknown',
        difficulty TEXT DEFAULT '',
        pool TEXT NOT NULL,
        seen_benchmark INTEGER NOT NULL DEFAULT 0,
        is_new_bank INTEGER NOT NULL DEFAULT 0,
        import_batch TEXT DEFAULT '',
        imported_at TEXT NOT NULL,
        provenance_json TEXT DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1
    );
    """)
    conn.execute(
        """INSERT INTO questions (fingerprint, source, correct_letter, pool, imported_at)
           VALUES ('fp-1', 'college_board_question_bank', 'B', 'fresh_training', '2026-01-01')""")
    conn.commit()
    yield conn
    conn.close()


def _draft(conn, qid=1, fp="fp-1"):
    return upsert_draft(
        conn, question_id=qid, question_fingerprint=fp,
        tested_task="inference", tempting_answer="overstates the evidence",
        exact_failure="adds 'always' where the passage says 'often'",
        correct_reasoning="the correct answer stays within the passage's scope",
        kb_tactic_refs=["kb/wiki/summaries/settele-strong-words.md"],
        error_taxonomy=["qualifier_strength"],
        evidence=[{"role": "stem", "text": "Which choice most strongly suggests"}],
        generator_notes={"confidence": "medium"},
    )


# ---------------------------------------------------------------------------
# Draft creation + duplicate prevention
# ---------------------------------------------------------------------------

def test_upsert_draft_creates_and_round_trips(db):
    rev = _draft(db)
    assert rev.state == DRAFT
    assert rev.question_id == 1
    assert rev.question_fingerprint == "fp-1"
    assert rev.exact_failure.startswith("adds 'always'")
    assert rev.kb_tactic_refs == ["kb/wiki/summaries/settele-strong-words.md"]
    assert rev.error_taxonomy == ["qualifier_strength"]
    assert rev.stale is False
    # Re-read from DB
    again = get_review(db, rev.id)
    assert again == rev


def test_upsert_draft_updates_existing_draft_in_place(db):
    first = _draft(db)
    second = _draft(db, fp="fp-1")
    assert first.id == second.id          # same row, not a duplicate
    rows = db.execute("SELECT COUNT(*) FROM question_reviews").fetchone()[0]
    assert rows == 1


def test_upsert_draft_refuses_to_overwrite_approved(db):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="looks good", actor="test")
    with pytest.raises(DuplicateReviewError):
        _draft(db)


def test_upsert_draft_unknown_question_raises(db):
    with pytest.raises(ValueError):
        upsert_draft(db, question_id=999, question_fingerprint="fp-x",
                     tested_task="t")


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_draft_to_approved_to_edited_to_rejected(db):
    rev = _draft(db)
    approved = transition(db, rev.id, APPROVED, reason="ok", actor="tester")
    assert approved.state == APPROVED
    assert approved.approved_at is not None
    edited = transition(db, rev.id, EDITED, reason="fix wording", actor="tester")
    assert edited.state == EDITED
    assert edited.approved_at is not None    # keeps original approval time
    rejected = transition(db, rev.id, REJECTED, reason="not useful", actor="tester")
    assert rejected.state == REJECTED


def test_invalid_transitions_raise(db):
    rev = _draft(db)
    # draft -> edited is not allowed
    with pytest.raises(ReviewStateError):
        transition(db, rev.id, EDITED)
    # rejected is terminal; nothing after
    transition(db, rev.id, REJECTED, reason="nope", actor="tester")
    with pytest.raises(ReviewStateError):
        transition(db, rev.id, APPROVED, reason="late", actor="tester")


def test_approve_requires_exact_failure(db):
    rev = upsert_draft(db, question_id=1, question_fingerprint="fp-1",
                       tested_task="t", tempting_answer="x", exact_failure="",
                       correct_reasoning="y")
    with pytest.raises(ReviewStateError):
        transition(db, rev.id, APPROVED, reason="ok", actor="tester")


def test_approve_stale_review_is_blocked(db):
    rev = _draft(db)
    # Simulate question fingerprint drift (question rebuilt / reimported).
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    with pytest.raises(ReviewStateError):
        transition(db, rev.id, APPROVED, reason="ok", actor="tester")


def test_provenance_records_edits(db):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="initial approval", actor="alice")
    edited = edit_review(db, rev.id, exact_failure="rewritten failure",
                         actor="bob", reason="clearer wording")
    notes = json.loads(edited.provenance_json)
    assert len(notes["edits"]) >= 2
    assert notes["edits"][-1]["type"] == "edit"
    assert notes["edits"][-1]["actor"] == "bob"
    assert notes["edits"][-1]["changes"]["exact_failure"]["to"] == "rewritten failure"


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def test_edit_changes_fields_and_logs(db):
    rev = _draft(db)
    edited = edit_review(db, rev.id, correct_reasoning="new reasoning",
                         actor="c", reason="improve")
    assert edited.correct_reasoning == "new reasoning"
    notes = json.loads(edited.provenance_json)
    assert any(e.get("type") == "edit" for e in notes["edits"])


def test_edit_noop_does_not_log(db):
    rev = _draft(db)
    # Passing empty strings means "no change" - no edit entry should be added.
    same = edit_review(db, rev.id, actor="c", reason="no-op")
    notes = json.loads(same.provenance_json)
    assert notes.get("edits", []) == []


# ---------------------------------------------------------------------------
# Stale references
# ---------------------------------------------------------------------------

def test_stale_flag_set_on_fingerprint_drift(db):
    rev = _draft(db)
    assert rev.stale is False
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    again = get_review(db, rev.id)
    assert again.stale is True


def test_list_reviews_filters_stale(db):
    _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    with_stale = list_reviews(db, include_stale=True)
    without_stale = list_reviews(db, include_stale=False)
    assert len(with_stale) == 1
    assert len(without_stale) == 0


def test_delete_review_draft_only(db):
    rev = _draft(db)
    delete_review(db, rev.id)
    assert db.execute("SELECT COUNT(*) FROM question_reviews").fetchone()[0] == 0


def test_delete_approved_requires_reject_first(db):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="ok", actor="t")
    with pytest.raises(ReviewStateError):
        delete_review(db, rev.id)


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def test_export_draft_is_blocked(db, tmp_path):
    rev = _draft(db)
    with pytest.raises(ReviewStateError):
        export_review(db, rev.id, vault_dir=tmp_path)


def test_export_approved_writes_diagnosis_only(db, tmp_path):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="ok", actor="t")
    target = export_review(db, rev.id, vault_dir=tmp_path)
    assert target.exists()
    text = target.read_text()
    # Diagnosis is present; canonical data is NOT duplicated.
    assert "adds 'always'" in text
    assert "question_fingerprint: fp-1" in text
    assert "question_id: 1" in text
    assert "exact failure" in text.lower()
    # The review does NOT carry the passage or choices.
    assert "Which choice most strongly suggests" not in text  # only evidence role, not full stem
    # KB tactic is linked vault-relative (the #35 convention).
    assert "[[summaries/settele-strong-words]]" in text


def test_export_sets_exported_at(db, tmp_path):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="ok", actor="t")
    assert rev.exported_at is None
    export_review(db, rev.id, vault_dir=tmp_path)
    again = get_review(db, rev.id)
    assert again.exported_at is not None


def test_export_dry_run_does_not_write(db, tmp_path):
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="ok", actor="t")
    target = export_review(db, rev.id, vault_dir=tmp_path, dry_run=True)
    assert not target.exists()
    assert "review-q1" in target.name


def test_export_stale_review_blocked(db, tmp_path):
    rev = _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    with pytest.raises(ReviewStateError):
        export_review(db, rev.id, vault_dir=tmp_path)


# ---------------------------------------------------------------------------
# SQLite-is-authoritative guarantee
# ---------------------------------------------------------------------------

def test_review_does_not_copy_canonical_data(db):
    """The review row stores only diagnosis + join keys. The passage,
    stem, choices, correct letter and rationale stay in questions."""
    rev = _draft(db)
    row = db.execute(
        "SELECT * FROM question_reviews WHERE id=?", (rev.id,)).fetchone()
    for col in ("passage", "stem", "choices_json", "correct_letter",
                "rationale"):
        assert col not in row.keys(), f"review must not carry canonical {col}"
    # The evidence_json is limited to {role, text} citations.
    evidence = json.loads(row["evidence_json"])
    assert all(set(c) <= {"role", "text", "letter"} for c in evidence)


def test_module_boundary_no_training_import():
    import satprep.reviews as rv
    src = pathlib.Path(rv.__file__).read_text()
    assert "from .training" not in src
    assert "import .training" not in src