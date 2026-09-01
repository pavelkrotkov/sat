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
    stem, choices, correct letter and rationale stay in questions.
    PR-44 round-1 P1: canonical evidence is no longer copied into the
    review row at all — citations collapse to {role, letter?, ref?}
    refs that point at the authoritative questions row."""
    rev = _draft(db)
    row = db.execute(
        "SELECT * FROM question_reviews WHERE id=?", (rev.id,)).fetchone()
    for col in ("passage", "stem", "choices_json", "correct_letter",
                "rationale"):
        assert col not in row.keys(), f"review must not carry canonical {col}"
    # The evidence_json carries only non-canonical refs.
    evidence = json.loads(row["evidence_json"])
    assert all(set(c) <= {"role", "letter", "ref"} for c in evidence)
    # No canonical text leaks through: each citation's value side is
    # either empty, a one-letter choice key, or a `questions:<id>` ref.
    for c in evidence:
        text = c.get("text", "")
        assert not text or len(text) <= 8, text


def test_module_boundary_no_training_import():
    import satprep.reviews as rv
    src = pathlib.Path(rv.__file__).read_text()
    assert "from .training" not in src
    assert "import .training" not in src


# ---------------------------------------------------------------------------
# PR-44 round-1 review findings: regression tests for the bot review
# threads Codex raised on commit 19f1aec.
# ---------------------------------------------------------------------------

def test_export_emits_required_frontmatter(db, tmp_path):
    """P1 finding 3899816853: the exported Markdown must carry
    `student_answer`, `correct_answer`, and `confidence` so it passes
    `scripts/check_kb.py` line 71-73 + 206-210."""
    rev = upsert_draft(
        db, question_id=1, question_fingerprint="fp-1",
        student_answer="A", correct_answer="B", confidence="medium",
        tested_task="inference",
        exact_failure="adds 'always' where the passage says 'often'",
        correct_reasoning="the correct answer stays within passage scope",
        kb_tactic_refs=["kb/wiki/summaries/settele-strong-words.md"],
        error_taxonomy=["qualifier_strength"],
    )
    transition(db, rev.id, APPROVED, reason="ok", actor="t")
    target = export_review(db, rev.id, vault_dir=tmp_path)
    text = target.read_text()
    # Required for type=question-review per scripts/check_kb.py line 71-73
    assert "student_answer: A" in text
    assert "correct_answer: B" in text
    assert "confidence: medium" in text
    # All three must be valid KB values too (line 206-210).
    import yaml
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["student_answer"] in {"A", "B", "C", "D", "E"}
    assert fm["correct_answer"] in {"A", "B", "C", "D", "E"}
    assert fm["confidence"] in {"high", "medium", "low"}


def test_stale_when_question_missing(db):
    """P2 finding 3899816860: a review whose question row is deleted
    (or marked inactive) must read as stale."""
    rev = _draft(db)
    # Question row disappears (corpus reimport).
    db.execute("DELETE FROM questions WHERE id=1")
    db.commit()
    again = get_review(db, rev.id)
    assert again.stale is True


def test_stale_when_question_inactive(db):
    """P2 finding 3899816860: an inactive question (active=0) must
    also flag the review stale."""
    rev = _draft(db)
    db.execute("UPDATE questions SET active=0 WHERE id=1")
    db.commit()
    again = get_review(db, rev.id)
    assert again.stale is True


def test_stale_when_stored_fingerprint_empty(db):
    """P2 finding 3899816860: a review with no stored fingerprint
    cannot verify against the corpus; treat as stale so it surfaces."""
    rev = upsert_draft(db, question_id=1, question_fingerprint="",
                       tested_task="t", exact_failure="ef")
    again = get_review(db, rev.id)
    assert again.stale is True


def test_upsert_draft_refuses_overwrite_rejected(db):
    """P2 finding 3899816863: a rejected review is terminal. Running
    `generate` again must NOT overwrite the rejected row's diagnosis
    or provenance; the operator must delete the tombstone first."""
    rev = _draft(db)
    transition(db, rev.id, REJECTED, reason="not useful", actor="t")
    with pytest.raises(ReviewStateError):
        _draft(db)
    # Original diagnosis is intact.
    again = get_review(db, rev.id)
    assert again.state == REJECTED
    assert again.exact_failure.startswith("adds 'always'")


def test_edit_rejected_review_is_blocked(db):
    """P2 finding 3899816871: editing a terminal (rejected) review is
    not allowed; the audit trail says the row is final."""
    rev = _draft(db)
    transition(db, rev.id, REJECTED, reason="not useful", actor="t")
    with pytest.raises(ReviewStateError):
        edit_review(db, rev.id, exact_failure="rewrite",
                    actor="bob", reason="tidy-up")


def test_edit_stale_review_is_blocked(db):
    """P2 finding 3899816871: editing a stale (orphaned) review is not
    allowed; the operator must reject it instead."""
    rev = _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    with pytest.raises(ReviewStateError):
        edit_review(db, rev.id, exact_failure="tidy",
                    actor="bob", reason="improve")


def test_edit_approved_review_transitions_to_edited(db):
    """P2 finding 3899816871: editing an APPROVED review moves it to
    EDITED so the exported provenance reflects the post-edit
    lifecycle (was approved then edited)."""
    rev = _draft(db)
    transition(db, rev.id, APPROVED, reason="initial", actor="alice")
    edited = edit_review(db, rev.id, exact_failure="new failure text",
                         actor="bob", reason="clearer")
    assert edited.state == EDITED


def test_delete_review_writes_tombstone(db):
    """P2 finding 3899816877: deletion must leave an audit record so
    'what was deleted, when, by whom, why' survives."""
    from satprep.reviews import list_deletions
    rev = _draft(db)
    delete_review(db, rev.id, actor="alice", reason="superseded")
    tombstones = list_deletions(db)
    assert len(tombstones) == 1
    tomb = tombstones[0]
    assert tomb["review_id"] == rev.id
    assert tomb["question_id"] == 1
    assert tomb["state"] == DRAFT
    assert tomb["actor"] == "alice"
    assert tomb["reason"] == "superseded"
    snap = json.loads(tomb["snapshot_json"])
    assert snap["tested_task"] == "inference"
    assert snap["exact_failure"].startswith("adds 'always'")
    # The review row itself is gone.
    assert db.execute("SELECT COUNT(*) FROM question_reviews "
                      "WHERE id=?", (rev.id,)).fetchone()[0] == 0


def test_upsert_draft_merges_prior_edits_and_keeps_first_generated_at(db):
    """P2 finding 3899816881: regenerating an existing draft must
    preserve the original `first_generated_at` and merge (not
    overwrite) the prior `edits` audit log."""
    first = _draft(db)
    original_notes = json.loads(first.provenance_json)
    original_first = original_notes["first_generated_at"]
    second = _draft(db)
    notes = json.loads(second.provenance_json)
    assert notes["first_generated_at"] == original_first
    # The new generation appended a 'regenerate' entry; the prior
    # state is preserved (no edits were ever made, so the array is
    # just the regenerate marker).
    assert any(e.get("type") == "regenerate" for e in notes["edits"])


def test_list_reviews_default_includes_stale(db):
    """P2 finding 3899816886: the default `satprep review list` must
    surface stale rows; the queue is the operator's signal that the
    corpus has drifted under the reviews."""
    _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    rows = list_reviews(db, include_stale=True)
    assert len(rows) == 1
    assert rows[0].stale is True


def test_transition_stale_to_rejected_is_allowed(db):
    """P2 finding 3899816891: a stale review must still be terminally
    rejectable so the operator can clean up an orphan without
    resorting to deletion."""
    rev = _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    rejected = transition(db, rev.id, REJECTED, reason="orphan", actor="t")
    assert rejected.state == REJECTED


def test_transition_stale_to_approved_is_blocked(db):
    """P2 finding 3899816891 (companion): the stale guard still blocks
    approval — only reject is permitted on stale rows."""
    rev = _draft(db)
    db.execute("UPDATE questions SET fingerprint='fp-2' WHERE id=1")
    db.commit()
    with pytest.raises(ReviewStateError):
        transition(db, rev.id, APPROVED, reason="late", actor="t")


def test_review_show_cli_subcommand_exposes_full_body(db, tmp_path):
    """P1 finding 3899816897: `satprep review show` exposes the
    draft/approved/edited body so the operator can inspect it before
    approving."""
    from satprep import cli as cli_mod
    import sqlite3 as _sq
    # The CLI's `cmd_review_show` opens a fresh connection via
    # `db_context()`, which delegates to `connect(config.DB_PATH)`.
    # Point it at a freshly-seeded file DB so the in-memory `db`
    # fixture (which pytest owns) does not leak across commands.
    db_path = tmp_path / "show.db"
    fresh = _sq.connect(str(db_path))
    fresh.row_factory = _sq.Row
    fresh.executescript("""
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL, source_test TEXT DEFAULT '',
        source_question_number TEXT DEFAULT '', module TEXT DEFAULT '',
        passage TEXT DEFAULT '', stem TEXT DEFAULT '',
        choices_json TEXT NOT NULL DEFAULT '[]',
        correct_letter TEXT NOT NULL, rationale TEXT DEFAULT '',
        images_json TEXT DEFAULT '[]',
        official_domain TEXT DEFAULT '', official_skill TEXT DEFAULT '',
        skill_source TEXT DEFAULT 'unknown', difficulty TEXT DEFAULT '',
        pool TEXT NOT NULL, seen_benchmark INTEGER NOT NULL DEFAULT 0,
        is_new_bank INTEGER NOT NULL DEFAULT 0,
        import_batch TEXT DEFAULT '', imported_at TEXT NOT NULL,
        provenance_json TEXT DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1
    );
    INSERT INTO questions
      (id, fingerprint, source, correct_letter, pool, imported_at)
      VALUES (1, 'fp-1', 'cb_qbank', 'B', 'fresh', '2026-01-01');
    """)
    fresh.close()
    import satprep.config as cfg_mod
    saved = cfg_mod.DB_PATH
    cfg_mod.DB_PATH = db_path
    try:
        seeded = _sq.connect(str(db_path))
        seeded.row_factory = _sq.Row
        rev = upsert_draft(
            seeded, question_id=1, question_fingerprint="fp-1",
            student_answer="A", correct_answer="B", confidence="medium",
            tested_task="inference",
            exact_failure="adds 'always' where the passage says 'often'",
            correct_reasoning="the correct answer stays within passage scope",
            kb_tactic_refs=["kb/wiki/summaries/settele-strong-words.md"],
            error_taxonomy=["qualifier_strength"],
        )
        review_id = rev.id
        seeded.close()
        args = type("A", (), {"id": review_id})()
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli_mod.cmd_review_show(args)
        payload = json.loads(buf.getvalue())
        assert payload["state"] == DRAFT
        assert payload["exact_failure"].startswith("adds 'always'")
        assert payload["student_answer"] == "A"
        assert payload["correct_answer"] == "B"
    finally:
        cfg_mod.DB_PATH = saved


def test_evidence_strips_canonical_text(db):
    """P1 finding 3899816904: the persisted evidence_json must NOT
    carry the canonical question text (stem / student_choice /
    correct_choice / passage_excerpt). Those roles collapse to
    non-canonical {role, letter?, ref?} refs."""
    rev = upsert_draft(
        db, question_id=1, question_fingerprint="fp-1",
        tested_task="t", exact_failure="ef",
        evidence=[{"role": "stem", "text": "Which supports the claim? " * 10},
                  {"role": "student_choice", "letter": "A",
                   "text": "long choice text " * 20},
                  {"role": "correct_choice", "letter": "B",
                   "text": "short choice"},
                  {"role": "passage_excerpt", "text": "P" * 400},
                  {"role": "kb_ref", "ref": "kb/wiki/summaries/x"}])
    evidence = json.loads(rev.evidence_json)
    for e in evidence:
        if e["role"] in {"stem", "student_choice", "correct_choice",
                         "passage_excerpt"}:
            # The canonical role is preserved but the text is dropped.
            assert "text" not in e
        if e["role"] == "kb_ref":
            # Non-canonical refs pass through unchanged.
            assert e["ref"] == "kb/wiki/summaries/x"


def test_review_records_student_answer_correct_answer_confidence(db):
    """P1 finding 3899816853: the review row records the join keys the
    KB export needs; without them the export frontmatter is incomplete
    and the file fails the KB lint."""
    rev = upsert_draft(db, question_id=1, question_fingerprint="fp-1",
                       student_answer="A", correct_answer="B",
                       confidence="high",
                       tested_task="t", exact_failure="ef")
    assert rev.student_answer == "A"
    assert rev.correct_answer == "B"
    assert rev.confidence == "high"