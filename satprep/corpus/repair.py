"""Repair Bluebook history ingestion (issue #49).

The historical corpus has three compounding defects this module repairs:

1. Correct-answer review snapshots put the answer list inside
   ``.question-panel ol.answer-options``; ``parse_snapshot`` only read
   ``.answer-panel ol``, so 432 of 479 R&W records were ingested with zero
   choices. The choices ARE in the saved HTML; they were never extracted.

2. ``ingest_bluebook`` fell back to the JSON record's ``answer_choices``
   only when the snapshot was *unusable*; a valid snapshot with an empty
   choice list won over the JSON fallback, which is usually empty too.

3. Questions are deduplicated by the global content fingerprint. Once
   choices were missing, identical passage+stem pairs collided and a
   source occurrence (with its test/module/question identity) silently
   disappeared.

Repair strategy (idempotent, preserves attempts/tags):

* Re-parse every historical record from its HTML snapshot (both layouts),
  merging snapshot fields with JSON-record fields independently per field:
  snapshot wins for passage/stem/choices/key/rationale when present,
  JSON fills any gap. Every missing field is counted and reported.
* An occurrence table (``bluebook_occurrences``) records every source
  occurrence with its stable UID, test/module/question placement and
  resolved question_id, so identical content at different placements no
  longer erases provenance.
* Old choice-less rows are reconciled *in place*: the same fingerprint is
  kept when the passage+stem are unchanged (adding choices changes the
  fingerprint, but re-keying would orphan attempts/tags/reviews). The
  stored fingerprint column is left as the legacy collision key; the
  occurrence table carries the placement identity. New fingerprints are
  only used for genuinely new questions.
* Re-running repair is a no-op: occurrences are upserted by UID and every
  write is guarded by the current stored content.

The command is exposed as ``satprep repair-bluebook``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .. import config
from ..clock import utc_now
from . import fingerprint as fpmod
from .parse_snapshot import ParsedQuestion, parse_snapshot
from .tagger import diagnose_attempt

LOG = logging.getLogger("satprep.repair")


# --------------------------------------------------------------------- merge -


def _letter(value: str | None) -> str:
    """Extract an answer letter; labels like 'Correct Answer: B' yield B."""
    if not value:
        return ""
    matches = re.findall(r"(?:^|[^A-Za-z])([A-H])(?:[^A-Za-z]|$)", str(value).upper())
    return matches[-1] if matches else ""


def _status_of(rec: dict) -> tuple[str, str]:
    """Return (student_letter, correct_letter) preferring snapshot data."""
    my = rec.get("my_answer") or ""
    student = (
        _letter(my.split(";")[0])
        if ";" in my
        else (_letter(my) if "correct" not in my.lower() else "")
    )
    correct = _letter(rec.get("correct_answer"))
    return student, correct


def merge_fields(parsed, rec: dict) -> tuple[dict, list[str]]:
    """Merge parsed-snapshot fields with the JSON record independently.

    Snapshot wins per field; JSON fills gaps. Returns the merged field dict
    plus audit warnings naming every field that is missing from BOTH sources
    (a genuinely unavailable field, reported rather than silently dropped).
    """
    warnings: list[str] = []

    passage = parsed.passage or rec.get("question_text") or ""
    stem = parsed.stem or ""
    choices = list(parsed.choices) or _json_choices(rec)
    correct_letter = parsed.correct_letter or _status_of(rec)[1]
    student_letter = parsed.student_letter or _status_of(rec)[0]
    rationale = parsed.rationale or rec.get("explanation") or ""
    images = rec.get("images") or []

    if not choices:
        warnings.append("no answer choices in snapshot or JSON record")
    if not correct_letter:
        warnings.append("no answer key in snapshot or JSON record")
    if not rationale:
        warnings.append("no rationale in snapshot or JSON record")
    if not stem:
        warnings.append("no stem in snapshot or JSON record")
    if not passage:
        warnings.append("no passage in snapshot or JSON record")

    return {
        "passage": passage,
        "stem": stem,
        "choices": choices,
        "correct_letter": correct_letter,
        "student_letter": student_letter,
        "rationale": rationale,
        "images": images,
    }, warnings


def _json_choices(rec: dict) -> list[dict]:
    """Choices from the JSON record's answer_choices list (best effort).

    The scraper stores choices as "A. text" strings when the structured
    extraction ran; the JSON fallback path in ingest historically produced
    {letter, text, is_correct} dicts from the raw answer_choices. Handle
    both shapes.
    """
    raw = rec.get("answer_choices") or []
    choices: list[dict] = []
    for i, item in enumerate(raw):
        letter = chr(ord("A") + i)
        if isinstance(item, dict):
            letter = item.get("letter") or letter
            text = item.get("text") or ""
        else:
            text = str(item)
            m = re.match(r"^\s*\(?([A-H])[\.\)]\s*(.*)$", text)
            if m:
                letter = m.group(1)
                text = m.group(2)
        text = text.strip()
        if text:
            choices.append({"letter": letter, "text": text, "is_correct": False})
    return choices


# ---------------------------------------------------------------- repair -----


def repair_bluebook(conn) -> dict:
    """Reconcile the historical corpus from outputs/ + artifacts/ (idempotent).

    Returns a stats dict with counts for every repair action so the CLI can
    report exactly what changed.
    """
    stats = {
        "records": 0,
        "snapshots_missing": 0,
        "snapshots_parsed": 0,
        "fields_warned": 0,
        "warnings": [],
        "occurrences_upserted": 0,
        "rows_updated": 0,
        "rows_inserted": 0,
        "duplicate_placements": 0,
    }
    if not config.BLUEBOOK_JSON.exists():
        LOG.warning("no %s; nothing to repair", config.BLUEBOOK_JSON)
        return stats

    records = json.loads(config.BLUEBOOK_JSON.read_text())
    if isinstance(records, dict):
        records = list(records.values())

    # Map existing historical rows by their provenance bluebook_uid so an
    # occurrence resolves to the SAME question_id across repair runs.
    uid_to_qid: dict[str, int] = {}
    for row in conn.execute(
        """SELECT id, provenance_json FROM questions WHERE pool='historical'"""
    ):
        try:
            prov = json.loads(row["provenance_json"] or "{}")
        except json.JSONDecodeError:
            continue
        uid = prov.get("bluebook_uid") or ""
        if uid:
            uid_to_qid.setdefault(uid, row["id"])

    for rec in records:
        if rec.get("subject_bucket") != config.SUBJECT:
            continue
        stats["records"] += 1
        uid = rec.get("uid") or ""
        snap_path = rec.get("html_snapshot_path") or ""
        parsed = None
        snap_file = Path(snap_path)
        if not snap_file.is_absolute():
            # Records store paths relative to the repo root (e.g.
            # artifacts/html/...); resolve so repair works from any cwd.
            snap_file = config.REPO_ROOT / snap_path
        if snap_path and snap_file.exists():
            try:
                parsed = parse_snapshot(snap_file.read_text(errors="ignore"))
                stats["snapshots_parsed"] += 1
            except Exception:
                LOG.warning("parse failed for %s", uid)
                parsed = None
        else:
            stats["snapshots_missing"] += 1
        merged, warnings = merge_fields(parsed, rec) if parsed else _merge_json_only(rec)
        stats["warnings"].extend(f"{uid}: {w}" for w in warnings)
        stats["fields_warned"] += len(warnings)

        correct_letter = merged["correct_letter"]
        if not correct_letter:
            # cannot establish a key; refuse to fabricate
            stats["warnings"].append(f"{uid}: no answer key; skipped")
            continue

        fp = fpmod.fingerprint(
            merged["passage"], merged["stem"], [c["text"] for c in merged["choices"]]
        )
        existing_qid = uid_to_qid.get(uid)
        if existing_qid is None:
            # New occurrence: insert a question row.
            existing_qid = _insert_question(conn, rec, merged, fp, uid, snap_path)
            stats["rows_inserted"] += 1
        else:
            changed = _reconcile_question(conn, existing_qid, rec, merged, fp)
            if changed:
                stats["rows_updated"] += 1

        # Record the occurrence with its resolved question_id.
        conn.execute(
            """INSERT INTO bluebook_occurrences
                 (bluebook_uid, test_name, module, question_number, subject,
                  fingerprint, question_id, answer_status, scraped_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bluebook_uid) DO UPDATE SET
                 test_name=excluded.test_name,
                 module=excluded.module,
                 question_number=excluded.question_number,
                 subject=excluded.subject,
                 fingerprint=excluded.fingerprint,
                 question_id=excluded.question_id,
                 answer_status=excluded.answer_status,
                 scraped_at=excluded.scraped_at""",
            (
                uid,
                rec.get("test_name") or "",
                rec.get("module") or "",
                str(rec.get("question_number") or ""),
                config.SUBJECT,
                fp,
                existing_qid,
                rec.get("answer_status") or "",
                rec.get("scraped_at") or "",
            ),
        )
        stats["occurrences_upserted"] += 1

        # Historical attempt (once per uid even after dedupe/re-runs).
        _ensure_historical_attempt(conn, existing_qid, rec, merged)

    # Post-pass: flag placements with more than one occurrence.
    stats["duplicate_placements"] = _count_duplicate_placements(conn)
    return stats


def _merge_json_only(rec: dict) -> tuple[dict, list[str]]:
    """Merge path when no snapshot exists: JSON record only."""
    parsed = ParsedQuestion()
    parsed.passage = rec.get("question_text") or ""
    parsed.choices = _json_choices(rec)
    parsed.correct_letter = _status_of(rec)[1]
    parsed.student_letter = _status_of(rec)[0]
    parsed.rationale = rec.get("explanation") or ""
    return merge_fields(parsed, rec)


def _insert_question(conn, rec: dict, merged: dict, fp: str, uid: str, snap_path: str) -> int:
    provenance = {
        "bluebook_uid": uid,
        "html_snapshot": snap_path or "",
        "test_name": rec.get("test_name"),
        "module": rec.get("module"),
        "scraped_at": rec.get("scraped_at"),
        "skill_from_metadata": rec.get("skill") or "",
        "domain_from_metadata": rec.get("domain") or "",
    }
    cur = conn.execute(
        """INSERT INTO questions
           (fingerprint, source, source_test, source_question_number, module,
            passage, stem, choices_json, correct_letter, rationale, images_json,
            official_domain, official_skill, skill_source, difficulty,
            pool, seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,'',?,?)""",
        (
            fp,
            "bluebook_test",
            rec.get("test_name") or "",
            str(rec.get("question_number") or ""),
            rec.get("module") or "",
            merged["passage"],
            merged["stem"],
            json.dumps(merged["choices"]),
            merged["correct_letter"],
            merged["rationale"],
            json.dumps(merged["images"]),
            rec.get("domain") or "",
            rec.get("skill") or "",
            "metadata" if rec.get("skill") else "unknown",
            "",  # difficulty unknown for bluebook history
            "historical",
            utc_now(),
            json.dumps(provenance),
        ),
    )
    conn.execute("INSERT INTO question_state (question_id) VALUES (?)", (cur.lastrowid,))
    return cur.lastrowid


def _reconcile_question(conn, qid: int, rec: dict, merged: dict, fp: str) -> bool:
    """Fill gaps on an existing historical row in place.

    Keeps the same question_id so attempts/tags/reviews stay attached; never
    re-keys by a fresh fingerprint (that would orphan the rows that reference
    this question). Returns True when any column actually changed.
    """
    row = conn.execute(
        "SELECT passage, stem, choices_json, correct_letter, rationale, images_json, provenance_json FROM questions WHERE id=?",
        (qid,),
    ).fetchone()
    if row is None:
        return False
    updates: list[tuple[str, str]] = []
    new_choices = json.dumps(merged["choices"])
    if row["choices_json"] != new_choices:
        updates.append(("choices_json", new_choices))
    if not row["correct_letter"] and merged["correct_letter"]:
        updates.append(("correct_letter", merged["correct_letter"]))
    if not row["passage"] and merged["passage"]:
        updates.append(("passage", merged["passage"]))
    if not row["stem"] and merged["stem"]:
        updates.append(("stem", merged["stem"]))
    if not row["rationale"] and merged["rationale"]:
        updates.append(("rationale", merged["rationale"]))
    new_images = json.dumps(merged["images"])
    if row["images_json"] != new_images:
        updates.append(("images_json", new_images))
    if updates:
        sets = ", ".join(f"{col}=?" for col, _ in updates)
        conn.execute(
            f"UPDATE questions SET {sets} WHERE id=?",
            [val for _, val in updates] + [qid],
        )
    return bool(updates)


def _ensure_historical_attempt(conn, qid: int, rec: dict, merged: dict) -> None:
    """Insert the historical attempt + error diagnosis once per uid."""
    correctness = _historical_correctness(rec)
    session_key = f"hist:{rec.get('uid')}"
    seen = conn.execute("SELECT 1 FROM attempts WHERE session_id=?", (session_key,)).fetchone()
    if correctness is not None and not seen:
        conn.execute(
            """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                     confidence, time_ms, mode, attempted_at)
               VALUES (?,?,?,?,0,0,'historical',?)""",
            (
                session_key,
                qid,
                merged["student_letter"],
                correctness,
                rec.get("scraped_at") or utc_now(),
            ),
        )
        conn.execute(
            """INSERT INTO question_state (question_id, due_at) VALUES (?, NULL)
               ON CONFLICT(question_id) DO NOTHING""",
            (qid,),
        )
        if correctness == 0 and merged["student_letter"] and merged["choices"]:
            diagnose_attempt(
                conn, qid, merged["choices"], merged["correct_letter"], merged["student_letter"]
            )


def _historical_correctness(rec: dict) -> int | None:
    """Correctness from the record; never fabricated from bare right/wrong."""
    status = rec.get("answer_status")
    if status in ("Correct", "Incorrect"):
        return 1 if status == "Correct" else 0
    my = (rec.get("my_answer") or "").lower()
    if "incorrect" in my:
        return 0
    if "correct" in my and rec.get("correct_answer"):
        return 1
    return None


def _count_duplicate_placements(conn) -> int:
    """Placements (test,module,question) with more than one source occurrence."""
    return conn.execute(
        """SELECT COUNT(*) FROM (
             SELECT test_name, module, question_number
             FROM bluebook_occurrences
             GROUP BY test_name, module, question_number
             HAVING COUNT(*) > 1
           )"""
    ).fetchone()[0]
