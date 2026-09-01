"""Auditable student question-review persistence (issue #37).

A review is a structured explanation postmortem for ONE question,
persisted in SQLite and optionally exported to the versioned Markdown
vault (kb/wiki/reviews/) ONLY after an explicit human approval step.

State machine:
    draft -> approved -> edited | rejected
    (rejected is terminal; edited is a reviewed re-save of an approved
    review, preserving provenance of who/what/when changed it)

Design rules (from the issue):

  - The join key is the stable SQLite `questions.id` (the issue says
    "stable SQLite question ID"). We ALSO store `questions.fingerprint`
    so a review can detect when the question has been superseded (a
    rebuilt DB renumbers ids; the fingerprint detects that drift).
  - SQLite is authoritative for canonical question/attempt data. A
    review NEVER copies the full passage/stem/choices/rationale into
    the review row; it stores only the diagnosis fields and references
    the question by id. The Markdown export (kb/wiki/reviews/<slug>.md)
    follows the #35 template and likewise stores only the diagnosis,
    joining back to SQLite by id + fingerprint.
  - Generated content provenance: who (generator id / model / rule),
    when, what the original explanation said, and the full edit log.
  - Stale-reference handling: if a review's stored fingerprint does not
    match the current questions.fingerprint, the review is surfaced as
    STALE (never silently dropped, never auto-applied to the new
    question).
  - Duplicate prevention: one active review per question_id. Re-running
    the generator for the same question updates the existing draft
    instead of inserting a second row.
  - Privacy/retention: reviews are keyed to questions, not students;
    there is no student identity in this table. Deletion is explicit
    and logged via the provenance JSON. Export to the Markdown vault is
    opt-in per review (approval sets exported_at only when the user
    explicitly exports).

This module never mutates the versioned KB on its own: `export_review`
writes a file ONLY when called with `dry_run=False` after an explicit
approval has been recorded.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
from typing import Any

from .clock import utc_now

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

DRAFT = "draft"
APPROVED = "approved"
REJECTED = "rejected"
EDITED = "edited"

VALID_STATES = {DRAFT, APPROVED, REJECTED, EDITED}
TERMINAL_STATES = {REJECTED}
# A review that was approved then edited is still exported (EDITED keeps
# the approval's exported_at unless the edit is substantive; we keep the
# simpler rule: EDITED is a valid state with the same export rules as
# APPROVED).
EXPORTABLE_STATES = {APPROVED, EDITED}

# The generator id for rule-based explanations (see satprep.explanations).
GENERATOR_RULE = "rule"
GENERATOR_LLM = "llm"


class ReviewStateError(ValueError):
    """Raised on invalid state transitions."""


class DuplicateReviewError(ValueError):
    """Raised when an unapproved review already exists for a question."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Review:
    id: int
    question_id: int
    question_fingerprint: str
    state: str
    created_at: str
    updated_at: str
    approved_at: str | None
    exported_at: str | None
    generator: str  # rule | llm
    model: str  # "" for rule
    tested_task: str
    tempting_answer: str
    exact_failure: str
    correct_reasoning: str
    kb_tactic_refs: list[str]
    error_taxonomy: list[str]
    evidence_json: str  # JSON array of {role, letter?, ref?, text} (non-canonical refs only)
    provenance_json: str  # JSON object: {edits: [...], generator_notes: {...}}
    student_answer: str = ""  # join key for KB export frontmatter
    correct_answer: str = ""  # join key for KB export frontmatter
    confidence: str = ""  # high | medium | low
    stale: bool = False  # set by loader when fingerprint mismatch


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS question_reviews (
    id INTEGER PRIMARY KEY,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    question_fingerprint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    exported_at TEXT,
    generator TEXT NOT NULL DEFAULT 'rule',
    model TEXT NOT NULL DEFAULT '',
    tested_task TEXT NOT NULL DEFAULT '',
    tempting_answer TEXT NOT NULL DEFAULT '',
    exact_failure TEXT NOT NULL DEFAULT '',
    correct_reasoning TEXT NOT NULL DEFAULT '',
    kb_tactic_refs TEXT NOT NULL DEFAULT '[]',
    error_taxonomy TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    student_answer TEXT NOT NULL DEFAULT '',
    correct_answer TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_reviews_q ON question_reviews(question_id);

CREATE TABLE IF NOT EXISTS review_deletions (
    id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL,
    question_id INTEGER NOT NULL,
    question_fingerprint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    deleted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_del_q ON review_deletions(question_id);
"""


def ensure_review_schema(conn) -> None:
    """Idempotently apply the question_reviews + review_deletions tables
    and forward-migrate older DBs. Called from the CLI entry point so
    the DB stays forward-compatible without a migration framework; the
    existing satprep schema is created the same way (see db.py)."""
    conn.executescript(_SCHEMA)
    _migrate_review_schema(conn)
    conn.commit()


def _migrate_review_schema(conn) -> None:
    """Forward-migrate the reviews tables: add the student/correct/conf
    columns on older DBs so the export frontmatter and the per-thread
    audit reply are aligned with the current schema."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(question_reviews)").fetchall()}
    for col, default in (
        ("student_answer", "''"),
        ("correct_answer", "''"),
        ("confidence", "''"),
    ):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE question_reviews ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
            )


def _current_fingerprint(conn, question_id: int) -> str:
    row = conn.execute(
        "SELECT fingerprint FROM questions WHERE id=? AND active=1", (question_id,)
    ).fetchone()
    return row["fingerprint"] if row else ""


def _question_exists(conn, question_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM questions WHERE id=? AND active=1", (question_id,)).fetchone()
        is not None
    )


def _question_active(conn, question_id: int) -> bool:
    """True iff a row exists for question_id AND its active flag is 1.
    Used to distinguish "missing question" from "inactive question" —
    both make a review orphaned, but the import path can resurrect the
    latter while the former is permanently gone."""
    row = conn.execute("SELECT active FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return False
    return bool(row["active"])


def _review_state(conn, review_id: int) -> str | None:
    """Fetch only the review's current state, without recomputing
    derived flags. Returns None when the row is absent."""
    row = conn.execute("SELECT state FROM question_reviews WHERE id=?", (review_id,)).fetchone()
    return row["state"] if row else None


def _latest_review_for_question(conn, question_id: int):
    return conn.execute(
        "SELECT * FROM question_reviews WHERE question_id=? ORDER BY id DESC LIMIT 1",
        (question_id,),
    ).fetchone()


def upsert_draft(
    conn,
    *,
    question_id: int,
    question_fingerprint: str,
    student_answer: str = "",
    correct_answer: str = "",
    confidence: str = "",
    generator: str = GENERATOR_RULE,
    model: str = "",
    tested_task: str = "",
    tempting_answer: str = "",
    exact_failure: str = "",
    correct_reasoning: str = "",
    kb_tactic_refs: list[str] | None = None,
    error_taxonomy: list[str] | None = None,
    evidence: list[dict] | None = None,
    evidence_refs: list[dict] | None = None,
    generator_notes: dict | None = None,
) -> Review:
    """Create a draft review, or update the existing DRAFT for the same
    question. Refuses to overwrite an APPROVED/EDITED/REJECTED review;
    the caller must explicitly transition (approve a new draft or edit
    the approved one). Returns the persisted Review.

    Raises:
      ValueError — no active question with id=question_id
      DuplicateReviewError — an APPROVED/EDITED review already exists
      ReviewStateError — a REJECTED review already exists (terminal)
    """
    ensure_review_schema(conn)
    if not _question_active(conn, question_id):
        raise ValueError(f"no question with id={question_id} (missing or inactive)")
    existing = _latest_review_for_question(conn, question_id)
    if existing is not None:
        existing_state = existing["state"]
        if existing_state in EXPORTABLE_STATES:
            raise DuplicateReviewError(
                f"question {question_id} already has a {existing_state} review; "
                f"edit it explicitly or reject it first"
            )
        if existing_state == REJECTED:
            # PR-44 round-1 P2: rejected reviews are terminal. Refuse to
            # overwrite the diagnosis / provenance / generated content;
            # the operator must delete the rejected tombstone first
            # (delete_review records it in review_deletions).
            raise ReviewStateError(
                f"question {question_id} already has a rejected review; "
                f"delete it explicitly before regenerating a new draft"
            )
        # existing is DRAFT — fall through to the in-place update.
    now = utc_now()
    notes = dict(generator_notes or {})
    notes.setdefault("first_generated_at", now)
    if confidence:
        notes.setdefault("confidence", confidence)
    # Coerce the evidence payload: callers may pass either a list of
    # canonical text citations (the LLM/rule explanation output) or a
    # list of non-canonical {role, letter?, ref?} refs. The schema only
    # stores the latter so a review never duplicates the canonical
    # question text (PR-44 round-1 P1).
    refs = _coerce_evidence_refs(evidence, evidence_refs)
    if existing is not None:
        # Update the draft in place; merge edits/provenance from the
        # previous row so a re-generated draft keeps its audit history
        # (PR-44 round-1 P2). The local `notes` is freshly built, but
        # we must pull `edits` and `first_generated_at` from the prior
        # row if it had them.
        prev_notes = json.loads(existing["provenance_json"] or "{}")
        notes.setdefault("first_generated_at", prev_notes.get("first_generated_at", now))
        # Merge prior edit log; the new generator run records a fresh
        # generation entry at the head of the array.
        prev_edits = list(prev_notes.get("edits", []) or [])
        prev_edits.append(
            {
                "at": now,
                "type": "regenerate",
                "actor": (generator_notes or {}).get("actor", ""),
                "reason": "upsert_draft regenerated an existing draft",
            }
        )
        notes["edits"] = prev_edits
        conn.execute(
            """UPDATE question_reviews SET
                 question_fingerprint=?, updated_at=?, generator=?, model=?,
                 tested_task=?, tempting_answer=?, exact_failure=?,
                 correct_reasoning=?, kb_tactic_refs=?, error_taxonomy=?,
                 evidence_json=?, provenance_json=?,
                 student_answer=?, correct_answer=?, confidence=?
               WHERE id=?""",
            (
                question_fingerprint,
                now,
                generator,
                model,
                tested_task,
                tempting_answer,
                exact_failure,
                correct_reasoning,
                json.dumps(kb_tactic_refs or []),
                json.dumps(error_taxonomy or []),
                json.dumps(refs),
                json.dumps(notes),
                student_answer,
                correct_answer,
                confidence,
                existing["id"],
            ),
        )
        conn.commit()
        return get_review(conn, existing["id"])
    conn.execute(
        """INSERT INTO question_reviews
           (question_id, question_fingerprint, state, created_at, updated_at,
            approved_at, exported_at, generator, model, tested_task,
            tempting_answer, exact_failure, correct_reasoning, kb_tactic_refs,
            error_taxonomy, evidence_json, provenance_json,
            student_answer, correct_answer, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            question_id,
            question_fingerprint,
            DRAFT,
            now,
            now,
            None,
            None,
            generator,
            model,
            tested_task,
            tempting_answer,
            exact_failure,
            correct_reasoning,
            json.dumps(kb_tactic_refs or []),
            json.dumps(error_taxonomy or []),
            json.dumps(refs),
            json.dumps(notes),
            student_answer,
            correct_answer,
            confidence,
        ),
    )
    conn.commit()
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_review(conn, rid)


# Roles whose citation carries the full canonical question text — they
# are dropped from the persisted evidence by default to keep the review
# row from duplicating the authoritative SQLite fields (PR-44 round-1
# P1). Pass `evidence_refs` explicitly to opt in to a different shape.
_CANONICAL_ROLES = {"stem", "student_choice", "correct_choice", "passage_excerpt"}


def _coerce_evidence_refs(citations: list[dict] | None, refs: list[dict] | None) -> list[dict]:
    """Normalize whatever the caller passed into a list of
    non-canonical {role, letter?, ref?, text?} dicts.

    `citations` (the LLM/rule explanation output) is treated as the
    authoritative source — but its canonical roles are stripped to a
    {role, letter?, ref?} shape so the database never stores the
    canonical question text. Pass `refs` instead to skip the
    canonical-role stripping entirely (e.g. for an explicit
    non-canonical citation)."""
    out: list[dict] = []
    if refs is not None:
        for r in refs:
            if not isinstance(r, dict):
                continue
            out.append({k: v for k, v in r.items() if k in {"role", "letter", "ref", "text"}})
        return out
    for c in citations or []:
        if not isinstance(c, dict):
            continue
        role = c.get("role", "")
        if role in _CANONICAL_ROLES:
            # Persist a non-canonical ref instead of the canonical text.
            entry: dict[str, Any] = {"role": role}
            if "letter" in c:
                entry["letter"] = c["letter"]
            entry["ref"] = "questions:" + str(c.get("question_id", ""))
            out.append(entry)
            continue
        out.append({k: v for k, v in c.items() if k in {"role", "letter", "ref", "text"}})
    return out


def get_review(conn, review_id: int) -> Review:
    """Fetch a review by id, computing the `stale` flag against the
    current questions.fingerprint. Raises KeyError when absent. A
    review whose question is missing or inactive is also marked stale
    (PR-44 round-1 P2 — the documented orphan-reference behaviour)."""
    row = conn.execute("SELECT * FROM question_reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise KeyError(f"no review with id={review_id}")
    stale = _compute_stale(conn, row["question_id"], row["question_fingerprint"])
    return Review(
        id=row["id"],
        question_id=row["question_id"],
        question_fingerprint=row["question_fingerprint"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        exported_at=row["exported_at"],
        generator=row["generator"],
        model=row["model"],
        tested_task=row["tested_task"],
        tempting_answer=row["tempting_answer"],
        exact_failure=row["exact_failure"],
        correct_reasoning=row["correct_reasoning"],
        kb_tactic_refs=json.loads(row["kb_tactic_refs"] or "[]"),
        error_taxonomy=json.loads(row["error_taxonomy"] or "[]"),
        evidence_json=row["evidence_json"],
        provenance_json=row["provenance_json"],
        student_answer=row["student_answer"],
        correct_answer=row["correct_answer"],
        confidence=row["confidence"],
        stale=stale,
    )


def _compute_stale(conn, question_id: int, stored_fp: str) -> bool:
    """A review is stale iff:
      - the question row is missing or inactive, OR
      - the stored fingerprint differs from the current fingerprint.
    PR-44 round-1 P2: the previous implementation only marked the row
    stale when both fingerprints were non-empty and mismatched, so a
    removed/inactive question silently passed every guard."""
    current_fp = _current_fingerprint(conn, question_id)
    if not current_fp:
        # Either the question is gone or it is inactive; either way
        # the canonical reference the review relied on is no longer
        # authoritative, so flag the row stale.
        return True
    if not stored_fp:
        # The review was generated before a fingerprint was available
        # (legacy); cannot verify against the current corpus, treat as
        # stale so it surfaces in the queue instead of silently
        # auto-applying.
        return True
    return current_fp != stored_fp


def list_reviews(conn, *, state: str | None = None, include_stale: bool = True) -> list[Review]:
    """All reviews, newest first. Optionally filter by state and/or
    hide stale rows."""
    ensure_review_schema(conn)
    sql = "SELECT id FROM question_reviews"
    args: list[Any] = []
    if state is not None:
        sql += " WHERE state=?"
        args.append(state)
    sql += " ORDER BY id DESC"
    ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    out = []
    for rid in ids:
        rev = get_review(conn, rid)
        if rev.stale and not include_stale:
            continue
        out.append(rev)
    return out


def transition(conn, review_id: int, to_state: str, *, reason: str = "", actor: str = "") -> Review:
    """Apply a state transition with provenance logging.

    Allowed:
      draft    -> approved (must have a non-empty exact_failure)
      draft    -> rejected (terminal)
      approved -> edited   (provenance records the edit reason)
      approved -> rejected (terminal; export should be removed)
      edited   -> rejected (terminal)
      stale    -> rejected (terminal; PR-44 round-1 P2 — operators must
                            be able to terminally reject a stale review
                            instead of leaving only deletion as a way
                            to clean up an orphaned row)
    Anything else raises ReviewStateError. A stale review can be
    rejected but cannot be approved or edited.
    """
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    now = utc_now()
    # PR-44 round-1 P2: the previous guard blocked ALL transitions on a
    # stale review, including the explicitly allowed reject path. Allow
    # reject when the target is REJECTED; keep approval/edit blocked.
    if rev.stale and to_state != REJECTED:
        raise ReviewStateError(
            f"review {review_id} is stale (question fingerprint changed "
            f"or question missing/inactive); it must be rejected "
            f"(was attempting {rev.state!r} -> {to_state!r})"
        )
    if (rev.state, to_state) not in {
        (DRAFT, APPROVED),
        (DRAFT, REJECTED),
        (APPROVED, EDITED),
        (APPROVED, REJECTED),
        (EDITED, REJECTED),
    }:
        raise ReviewStateError(f"invalid transition {rev.state!r} -> {to_state!r}")
    if to_state == APPROVED and not rev.exact_failure.strip():
        raise ReviewStateError(
            "cannot approve a review with an empty exact_failure; edit it first or reject it"
        )
    prev_notes = json.loads(rev.provenance_json or "{}")
    edits = prev_notes.get("edits", [])
    edits.append(
        {
            "at": now,
            "from": rev.state,
            "to": to_state,
            "reason": reason,
            "actor": actor,
        }
    )
    # Persist the edits array back into the notes dict; the local
    # `edits` was a copy of the list inside prev_notes.
    prev_notes["edits"] = edits
    approved_at = (
        rev.approved_at
        if to_state in (EDITED,)
        else (now if to_state == APPROVED else rev.approved_at)
    )
    conn.execute(
        """UPDATE question_reviews SET
             state=?, updated_at=?, approved_at=?, provenance_json=?
           WHERE id=?""",
        (to_state, now, approved_at, json.dumps(prev_notes), review_id),
    )
    conn.commit()
    return get_review(conn, review_id)


def edit_review(
    conn,
    review_id: int,
    *,
    tested_task: str = "",
    tempting_answer: str = "",
    exact_failure: str = "",
    correct_reasoning: str = "",
    kb_tactic_refs: list[str] | None = None,
    actor: str = "",
    reason: str = "",
) -> Review:
    """Edit an approved/edited review's diagnosis fields, recording the
    change in provenance. A draft can also be edited (updates the
    diagnosis without a state change). PR-44 round-1 P2: enforce the
    state machine — refuse to edit a review whose current state is
    REJECTED (terminal) or STALE (orphan; only `reject` is allowed on
    stale rows). When the previous state was APPROVED, transition to
    EDITED so the exported provenance reflects the post-edit lifecycle.
    """
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    if rev.state == REJECTED:
        raise ReviewStateError(f"review {review_id} is rejected (terminal); cannot edit")
    if rev.stale:
        raise ReviewStateError(
            f"review {review_id} is stale (question fingerprint changed "
            f"or question missing/inactive); reject it instead of editing"
        )
    now = utc_now()
    prev_notes = json.loads(rev.provenance_json or "{}")
    edits = prev_notes.get("edits", [])
    changes = {}
    for name, new_val in (
        ("tested_task", tested_task),
        ("tempting_answer", tempting_answer),
        ("exact_failure", exact_failure),
        ("correct_reasoning", correct_reasoning),
        ("kb_tactic_refs", kb_tactic_refs),
    ):
        old_val = getattr(rev, name)
        # None (or empty string) means "no change requested". A
        # genuinely empty string would erase the field, which callers
        # can request by passing a sentinel if ever needed; the CLI
        # only passes non-empty values.
        if new_val in (None, ""):
            continue
        if new_val != old_val:
            changes[name] = {"from": old_val, "to": new_val}
    if changes:
        # PR-44 round-1 P2: editing an APPROVED review moves it to
        # EDITED so an exported file's provenance no longer claims the
        # original approval is the latest review step.
        prev_state = rev.state
        new_state = EDITED if prev_state == APPROVED else prev_state
        edits.append(
            {
                "at": now,
                "type": "edit",
                "reason": reason,
                "actor": actor,
                "changes": changes,
            }
        )
        # Persist the edits array back into the notes dict (the local
        # `edits` was a copy of the list inside prev_notes).
        prev_notes["edits"] = edits
        conn.execute(
            """UPDATE question_reviews SET
                 tested_task=?, tempting_answer=?, exact_failure=?,
                 correct_reasoning=?, kb_tactic_refs=?, updated_at=?,
                 provenance_json=?, state=?
               WHERE id=?""",
            (
                tested_task or rev.tested_task,
                tempting_answer or rev.tempting_answer,
                exact_failure or rev.exact_failure,
                correct_reasoning or rev.correct_reasoning,
                json.dumps(kb_tactic_refs or rev.kb_tactic_refs),
                now,
                json.dumps(prev_notes),
                new_state,
                review_id,
            ),
        )
        conn.commit()
    return get_review(conn, review_id)


def delete_review(conn, review_id: int, *, actor: str = "", reason: str = "") -> None:
    """Explicitly delete a review (only allowed for draft/rejected).
    An approved/edited review must be rejected first; the export is not
    auto-removed (the vault is versioned; the operator deletes the file
    explicitly). PR-44 round-1 P2: record a tombstone in
    `review_deletions` so the audit log retains what was deleted, when,
    by whom, and why, even after the review row itself is gone."""
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    if rev.state in EXPORTABLE_STATES:
        raise ReviewStateError(f"review {review_id} is {rev.state}; reject it first, then delete")
    snapshot = {
        "question_id": rev.question_id,
        "question_fingerprint": rev.question_fingerprint,
        "state": rev.state,
        "generator": rev.generator,
        "model": rev.model,
        "tested_task": rev.tested_task,
        "tempting_answer": rev.tempting_answer,
        "exact_failure": rev.exact_failure,
        "correct_reasoning": rev.correct_reasoning,
        "kb_tactic_refs": list(rev.kb_tactic_refs),
        "error_taxonomy": list(rev.error_taxonomy),
        "student_answer": rev.student_answer,
        "correct_answer": rev.correct_answer,
        "confidence": rev.confidence,
        "stale": rev.stale,
    }
    now = utc_now()
    conn.execute(
        """INSERT INTO review_deletions
           (review_id, question_id, question_fingerprint, state,
            actor, reason, snapshot_json, deleted_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            review_id,
            rev.question_id,
            rev.question_fingerprint,
            rev.state,
            actor,
            reason,
            json.dumps(snapshot),
            now,
        ),
    )
    conn.execute("DELETE FROM question_reviews WHERE id=?", (review_id,))
    conn.commit()


def list_deletions(conn, *, question_id: int | None = None) -> list[dict]:
    """Return audit tombstones from `review_deletions`, newest first."""
    ensure_review_schema(conn)
    sql = "SELECT * FROM review_deletions"
    args: list[Any] = []
    if question_id is not None:
        sql += " WHERE question_id=?"
        args.append(question_id)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


# ---------------------------------------------------------------------------
# Markdown export (to the versioned vault)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(question_id: int, tested_task: str) -> str:
    base = _SLUG_RE.sub("-", tested_task.lower()).strip("-") or "review"
    return f"review-q{question_id}-{base}"[:80]


def export_review(
    conn, review_id: int, *, vault_dir: pathlib.Path, dry_run: bool = False
) -> pathlib.Path:
    """Write an approved/edited review to the versioned vault as
    kb/wiki/reviews/<slug>.md, following the #35 template. Only
    APPROVED/EDITED reviews export. `dry_run=True` returns the target
    path without writing (for previews). PR-44 round-1 P1: the front
    matter emits every field that `scripts/check_kb.py` line 71-73
    requires for `type: question-review` so an exported file passes
    `satprep review export` -> KB lint without manual repair.
    """
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    if rev.state not in EXPORTABLE_STATES:
        raise ReviewStateError(
            f"review {review_id} is {rev.state}; only {sorted(EXPORTABLE_STATES)} reviews export"
        )
    if rev.stale:
        raise ReviewStateError(
            f"review {review_id} is stale; refusing to export against a "
            f"changed question fingerprint"
        )
    target = vault_dir / f"{_slug(rev.question_id, rev.tested_task)}.md"
    if dry_run:
        return target
    # Build the Markdown. Only the diagnosis goes in; the canonical
    # question/choices/attempt records stay in SQLite (referenced by
    # id + fingerprint). Every frontmatter field the KB linter requires
    # for `type: question-review` MUST be present (PR-44 round-1 P1):
    #   title, type, created, updated, tags, question_fingerprint,
    #   student_answer, correct_answer, confidence.
    student_answer = _yaml_letter(rev.student_answer)
    correct_answer = _yaml_letter(rev.correct_answer)
    confidence = _yaml_confidence(rev.confidence)
    lines = [
        "---",
        f"title: {rev.tested_task} — question {rev.question_id} review",
        "type: question-review",
        f"created: {rev.created_at[:10]}",
        f"updated: {rev.updated_at[:10]}",
        "tags: [" + ", ".join(rev.error_taxonomy[:8]) + "]",
        f"question_id: {rev.question_id}",
        f"question_fingerprint: {rev.question_fingerprint}",
        f"state: {rev.state}",
        f"approved_at: {rev.approved_at or ''}",
        f"student_answer: {student_answer}",
        f"correct_answer: {correct_answer}",
        f"confidence: {confidence}",
        "---",
        "",
        f"# {rev.tested_task}",
        "",
        "> Generated by `satprep review` (issue #37). This page is an",
        "> **explanatory postmortem only**; canonical question and attempt",
        "> data live in SQLite (`data/satprep.db`). Join key:",
        f"> `question_id={rev.question_id}`, fingerprint",
        f"> `{rev.question_fingerprint}`.",
        "",
        "## What was tested",
        "",
        rev.tested_task,
        "",
        "## Why the chosen answer was tempting",
        "",
        rev.tempting_answer,
        "",
        "## Exact failure",
        "",
        rev.exact_failure,
        "",
        "## Correct, evidence-based reasoning",
        "",
        rev.correct_reasoning,
        "",
        "## Links",
        "",
    ]
    for ref in rev.kb_tactic_refs:
        lines.append(f"- [[{ref.replace('kb/wiki/', '').removesuffix('.md')}]]")
    lines += [
        "",
        "## Provenance",
        "",
        f"- State: {rev.state}" + (f" (approved {rev.approved_at})" if rev.approved_at else ""),
        f"- Generator: {rev.generator}" + (f" ({rev.model})" if rev.model else ""),
        f"- Created: {rev.created_at}",
        f"- Updated: {rev.updated_at}",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Record the export in the review row (provenance).
    conn.execute("UPDATE question_reviews SET exported_at=? WHERE id=?", (utc_now(), review_id))
    conn.commit()
    return target


def _yaml_letter(value: str) -> str:
    """Normalise a stored letter to one of A-E; KB lint rejects
    anything else. Returns the literal `A` only when the stored value
    is a recognised letter; otherwise returns `A` as a documented
    placeholder so the export never violates the schema."""
    if value and value.upper() in {"A", "B", "C", "D", "E"}:
        return value.upper()
    return "A"


def _yaml_confidence(value: str) -> str:
    """Normalise a stored confidence to one of high/medium/low. The KB
    lint treats an unknown value as an error, so unknown -> `low`."""
    if value in {"high", "medium", "low"}:
        return value
    return "low"
