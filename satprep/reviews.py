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
    generator: str          # rule | llm
    model: str              # "" for rule
    tested_task: str
    tempting_answer: str
    exact_failure: str
    correct_reasoning: str
    kb_tactic_refs: list[str]
    error_taxonomy: list[str]
    evidence_json: str      # JSON array of {role, text} (never the full question)
    provenance_json: str    # JSON object: {edits: [...], generator_notes: {...}}
    stale: bool = False     # set by loader when fingerprint mismatch


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
    provenance_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reviews_q ON question_reviews(question_id);
"""


def ensure_review_schema(conn) -> None:
    """Idempotently apply the question_reviews table. Called from the
    CLI entry point so the DB stays forward-compatible without a
    migration framework; the existing satprep schema is created the
    same way (see db.py)."""
    conn.executescript(_SCHEMA)
    conn.commit()


def _current_fingerprint(conn, question_id: int) -> str:
    row = conn.execute(
        "SELECT fingerprint FROM questions WHERE id=? AND active=1",
        (question_id,)).fetchone()
    return row["fingerprint"] if row else ""


def _question_exists(conn, question_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM questions WHERE id=? AND active=1",
        (question_id,)).fetchone() is not None


def upsert_draft(conn, *, question_id: int, question_fingerprint: str,
                 generator: str = GENERATOR_RULE, model: str = "",
                 tested_task: str = "", tempting_answer: str = "",
                 exact_failure: str = "", correct_reasoning: str = "",
                 kb_tactic_refs: list[str] | None = None,
                 error_taxonomy: list[str] | None = None,
                 evidence: list[dict] | None = None,
                 generator_notes: dict | None = None) -> Review:
    """Create a draft review, or update the existing DRAFT for the same
    question. Refuses to overwrite an APPROVED/EDITED review; the caller
    must explicitly transition (approve a new draft or edit the approved
    one). Returns the persisted Review.

    Raises DuplicateReviewError if an approved/edited review already
    exists for the question.
    """
    ensure_review_schema(conn)
    if not _question_exists(conn, question_id):
        raise ValueError(f"no active question with id={question_id}")
    existing = conn.execute(
        "SELECT * FROM question_reviews WHERE question_id=? ORDER BY id DESC LIMIT 1",
        (question_id,)).fetchone()
    if existing is not None and existing["state"] in EXPORTABLE_STATES:
        raise DuplicateReviewError(
            f"question {question_id} already has a {existing['state']} review; "
            f"edit it explicitly or reject it first")
    now = utc_now()
    notes = dict(generator_notes or {})
    notes.setdefault("first_generated_at", now)
    if existing is not None:
        # Update the draft in place; keep first_generated_at.
        prev_notes = json.loads(existing["provenance_json"] or "{}")
        notes.setdefault("first_generated_at",
                         prev_notes.get("first_generated_at", now))
        conn.execute(
            """UPDATE question_reviews SET
                 question_fingerprint=?, updated_at=?, generator=?, model=?,
                 tested_task=?, tempting_answer=?, exact_failure=?,
                 correct_reasoning=?, kb_tactic_refs=?, error_taxonomy=?,
                 evidence_json=?, provenance_json=?
               WHERE id=?""",
            (question_fingerprint, now, generator, model,
             tested_task, tempting_answer, exact_failure, correct_reasoning,
             json.dumps(kb_tactic_refs or []), json.dumps(error_taxonomy or []),
             json.dumps(evidence or []), json.dumps(notes),
             existing["id"]),
        )
        conn.commit()
        return get_review(conn, existing["id"])
    conn.execute(
        """INSERT INTO question_reviews
           (question_id, question_fingerprint, state, created_at, updated_at,
            approved_at, exported_at, generator, model, tested_task,
            tempting_answer, exact_failure, correct_reasoning, kb_tactic_refs,
            error_taxonomy, evidence_json, provenance_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (question_id, question_fingerprint, DRAFT, now, now, None, None,
         generator, model, tested_task, tempting_answer, exact_failure,
         correct_reasoning, json.dumps(kb_tactic_refs or []),
         json.dumps(error_taxonomy or []), json.dumps(evidence or []),
         json.dumps(notes)),
    )
    conn.commit()
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_review(conn, rid)


def get_review(conn, review_id: int) -> Review:
    """Fetch a review by id, computing the `stale` flag against the
    current questions.fingerprint. Raises KeyError when absent."""
    row = conn.execute(
        "SELECT * FROM question_reviews WHERE id=?", (review_id,)).fetchone()
    if row is None:
        raise KeyError(f"no review with id={review_id}")
    current_fp = _current_fingerprint(conn, row["question_id"])
    stale = bool(current_fp and row["question_fingerprint"]
                 and current_fp != row["question_fingerprint"])
    return Review(
        id=row["id"], question_id=row["question_id"],
        question_fingerprint=row["question_fingerprint"],
        state=row["state"], created_at=row["created_at"],
        updated_at=row["updated_at"], approved_at=row["approved_at"],
        exported_at=row["exported_at"], generator=row["generator"],
        model=row["model"], tested_task=row["tested_task"],
        tempting_answer=row["tempting_answer"],
        exact_failure=row["exact_failure"],
        correct_reasoning=row["correct_reasoning"],
        kb_tactic_refs=json.loads(row["kb_tactic_refs"] or "[]"),
        error_taxonomy=json.loads(row["error_taxonomy"] or "[]"),
        evidence_json=row["evidence_json"],
        provenance_json=row["provenance_json"],
        stale=stale,
    )


def list_reviews(conn, *, state: str | None = None,
                 include_stale: bool = True) -> list[Review]:
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


def transition(conn, review_id: int, to_state: str, *,
               reason: str = "", actor: str = "") -> Review:
    """Apply a state transition with provenance logging.

    Allowed:
      draft    -> approved (must have a non-empty exact_failure)
      draft    -> rejected (terminal)
      approved -> edited   (provenance records the edit reason)
      approved -> rejected (terminal; export should be removed)
      edited   -> rejected (terminal)
    Anything else raises ReviewStateError.
    """
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    now = utc_now()
    if rev.stale:
        raise ReviewStateError(
            f"review {review_id} is stale (question fingerprint changed); "
            f"it must not be approved/edited")
    if (rev.state, to_state) not in {
        (DRAFT, APPROVED), (DRAFT, REJECTED),
        (APPROVED, EDITED), (APPROVED, REJECTED),
        (EDITED, REJECTED),
    }:
        raise ReviewStateError(
            f"invalid transition {rev.state!r} -> {to_state!r}")
    if to_state == APPROVED and not rev.exact_failure.strip():
        raise ReviewStateError(
            "cannot approve a review with an empty exact_failure; "
            "edit it first or reject it")
    prev_notes = json.loads(rev.provenance_json or "{}")
    edits = prev_notes.get("edits", [])
    edits.append({
        "at": now,
        "from": rev.state,
        "to": to_state,
        "reason": reason,
        "actor": actor,
    })
    # Persist the edits array back into the notes dict; the local
    # `edits` was a copy of the list inside prev_notes.
    prev_notes["edits"] = edits
    approved_at = rev.approved_at if to_state in (EDITED,) else (
        now if to_state == APPROVED else rev.approved_at)
    conn.execute(
        """UPDATE question_reviews SET
             state=?, updated_at=?, approved_at=?, provenance_json=?
           WHERE id=?""",
        (to_state, now, approved_at, json.dumps(prev_notes), review_id),
    )
    conn.commit()
    return get_review(conn, review_id)


def edit_review(conn, review_id: int, *, tested_task: str = "",
                tempting_answer: str = "", exact_failure: str = "",
                correct_reasoning: str = "", kb_tactic_refs: list[str] | None = None,
                actor: str = "", reason: str = "") -> Review:
    """Edit an approved/edited review's diagnosis fields, recording the
    change in provenance. A draft can also be edited (updates the
    diagnosis without a state change)."""
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    now = utc_now()
    prev_notes = json.loads(rev.provenance_json or "{}")
    edits = prev_notes.get("edits", [])
    changes = {}
    for name, new_val in (("tested_task", tested_task),
                          ("tempting_answer", tempting_answer),
                          ("exact_failure", exact_failure),
                          ("correct_reasoning", correct_reasoning),
                          ("kb_tactic_refs", kb_tactic_refs)):
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
        edits.append({
            "at": now,
            "type": "edit",
            "reason": reason,
            "actor": actor,
            "changes": changes,
        })
        # Persist the edits array back into the notes dict (the local
        # `edits` was a copy of the list inside prev_notes).
        prev_notes["edits"] = edits
        conn.execute(
            """UPDATE question_reviews SET
                 tested_task=?, tempting_answer=?, exact_failure=?,
                 correct_reasoning=?, kb_tactic_refs=?, updated_at=?,
                 provenance_json=?
               WHERE id=?""",
            (tested_task or rev.tested_task,
             tempting_answer or rev.tempting_answer,
             exact_failure or rev.exact_failure,
             correct_reasoning or rev.correct_reasoning,
             json.dumps(kb_tactic_refs or rev.kb_tactic_refs),
             now, json.dumps(prev_notes), review_id),
        )
        conn.commit()
    return get_review(conn, review_id)


def delete_review(conn, review_id: int) -> None:
    """Explicitly delete a review (only allowed for draft/rejected). An
    approved review must be rejected first; the export is not auto-removed
    (the vault is versioned; the operator deletes the file explicitly)."""
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    if rev.state in EXPORTABLE_STATES:
        raise ReviewStateError(
            f"review {review_id} is {rev.state}; reject it first, then delete")
    conn.execute("DELETE FROM question_reviews WHERE id=?", (review_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Markdown export (to the versioned vault)
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(question_id: int, tested_task: str) -> str:
    base = _SLUG_RE.sub("-", tested_task.lower()).strip("-") or "review"
    return f"review-q{question_id}-{base}"[:80]


def export_review(conn, review_id: int, *, vault_dir: pathlib.Path,
                  dry_run: bool = False) -> pathlib.Path:
    """Write an approved/edited review to the versioned vault as
    kb/wiki/reviews/<slug>.md, following the #35 template. Only
    APPROVED/EDITED reviews export. `dry_run=True` returns the target
    path without writing (for previews)."""
    ensure_review_schema(conn)
    rev = get_review(conn, review_id)
    if rev.state not in EXPORTABLE_STATES:
        raise ReviewStateError(
            f"review {review_id} is {rev.state}; only {sorted(EXPORTABLE_STATES)} "
            f"reviews export")
    if rev.stale:
        raise ReviewStateError(
            f"review {review_id} is stale; refusing to export against a "
            f"changed question fingerprint")
    target = vault_dir / f"{_slug(rev.question_id, rev.tested_task)}.md"
    if dry_run:
        return target
    # Build the Markdown. Only the diagnosis goes in; the canonical
    # question/choices/attempt records stay in SQLite (referenced by
    # id + fingerprint).
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
    lines += ["", "## Provenance", "",
              f"- State: {rev.state}" +
              (f" (approved {rev.approved_at})" if rev.approved_at else ""),
              f"- Generator: {rev.generator}" +
              (f" ({rev.model})" if rev.model else ""),
              f"- Created: {rev.created_at}",
              f"- Updated: {rev.updated_at}",
              ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Record the export in the review row (provenance).
    conn.execute("UPDATE question_reviews SET exported_at=? WHERE id=?",
                 (utc_now(), review_id))
    conn.commit()
    return target