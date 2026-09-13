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
* Old choice-less rows are reconciled *in place*: the same question_id is
  kept so attempts/tags/reviews stay attached, and the stored fingerprint is
  brought in line with the corrected content when that fingerprint is not
  already owned by another row (a genuine content collision keeps its row
  identity and is tracked by the occurrence table).
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
    correct_letter = parsed.correct_letter or _status_of(rec)[1]
    student_letter = parsed.student_letter or _status_of(rec)[0]
    # Choices seen per-component so the JSON fallback marks the key (T8).
    choices = list(parsed.choices) or _json_choices(rec, correct_letter)
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


def _json_choices(rec: dict, correct_letter: str = "") -> list[dict]:
    """Choices from the JSON record's answer_choices list (best effort).

    The scraper stores choices as "A. text" strings when the structured
    extraction ran; the JSON fallback path in ingest historically produced
    {letter, text, is_correct} dicts from the raw answer_choices. Handle
    both shapes and mark the key from ``correct_letter`` so a recovered
    choice list never carries a valid ``correct_letter`` while ``is_correct``
    is false on every option.
    """
    raw = rec.get("answer_choices") or []
    choices: list[dict] = []
    for i, item in enumerate(raw):
        letter = chr(ord("A") + i)
        if isinstance(item, dict):
            letter = str(item.get("letter") or letter)
            text = str(item.get("text") or "")
        else:
            text = str(item)
            m = re.match(r"^\s*\(?([A-H])[\.\)]\s*(.*)$", text)
            if m:
                letter = m.group(1)
                text = m.group(2)
        text = text.strip()
        if text:
            choices.append(
                {
                    "letter": letter,
                    "text": text,
                    "is_correct": letter.strip().upper()
                    == str(correct_letter or "").strip().upper(),
                }
            )
    return choices


# ---------------------------------------------------------------- repair -----


def _new_repair_stats() -> dict:
    return {
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


def _historical_uid_map(conn) -> dict[str, int]:
    uid_to_qid: dict[str, int] = {}
    for row in conn.execute("SELECT id, provenance_json FROM questions WHERE pool='historical'"):
        try:
            uid = json.loads(row["provenance_json"] or "{}").get("bluebook_uid") or ""
        except json.JSONDecodeError:
            continue
        if uid:
            uid_to_qid.setdefault(uid, row["id"])
    return uid_to_qid


def _load_snapshot(rec: dict, stats: dict) -> tuple[ParsedQuestion | None, str]:
    uid = rec.get("uid") or ""
    snap_path = rec.get("html_snapshot_path") or ""
    snap_file = Path(snap_path)
    if not snap_file.is_absolute():
        snap_file = config.REPO_ROOT / snap_path
    if not snap_path or not snap_file.exists():
        stats["snapshots_missing"] += 1
        return None, snap_path
    try:
        parsed = parse_snapshot(snap_file.read_text(errors="ignore"))
    except Exception:
        LOG.warning("parse failed for %s", uid)
        return None, snap_path
    stats["snapshots_parsed"] += 1
    return parsed, snap_path


def _resolve_question_id(
    conn, uid_to_qid: dict[str, int], uid: str, fingerprint: str
) -> int | None:
    existing_qid = uid_to_qid.get(uid)
    if existing_qid is not None:
        return existing_qid
    row = conn.execute("SELECT id FROM questions WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row is None:
        return None
    uid_to_qid[uid] = row["id"]
    return row["id"]


def _upsert_occurrence(conn, rec: dict, uid: str, fingerprint: str, qid: int) -> None:
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
            fingerprint,
            qid,
            rec.get("answer_status") or "",
            rec.get("scraped_at") or "",
        ),
    )


def _repair_record(conn, rec: dict, stats: dict, uid_to_qid: dict[str, int]) -> None:
    if rec.get("subject_bucket") != config.SUBJECT:
        return
    stats["records"] += 1
    uid = rec.get("uid") or ""
    parsed, snap_path = _load_snapshot(rec, stats)
    merged, warnings = merge_fields(parsed, rec) if parsed else _merge_json_only(rec)
    stats["warnings"].extend(f"{uid}: {warning}" for warning in warnings)
    stats["fields_warned"] += len(warnings)
    if not merged["correct_letter"]:
        stats["warnings"].append(f"{uid}: no answer key; skipped")
        return
    fingerprint = fpmod.fingerprint(
        merged["passage"], merged["stem"], [choice["text"] for choice in merged["choices"]]
    )
    qid = _resolve_question_id(conn, uid_to_qid, uid, fingerprint)
    if qid is None:
        qid = _insert_question(conn, rec, merged, fingerprint, uid, snap_path)
        stats["rows_inserted"] += 1
    elif _reconcile_question(conn, qid, rec, merged, fingerprint):
        stats["rows_updated"] += 1
    _upsert_occurrence(conn, rec, uid, fingerprint, qid)
    stats["occurrences_upserted"] += 1
    _ensure_historical_attempt(conn, qid, rec, merged)


def repair_bluebook(conn) -> dict:
    """Reconcile the historical corpus from outputs/ + artifacts/ (idempotent).

    Returns a stats dict with counts for every repair action so the CLI can
    report exactly what changed.
    """
    stats = _new_repair_stats()
    if not config.BLUEBOOK_JSON.exists():
        LOG.warning("no %s; nothing to repair", config.BLUEBOOK_JSON)
        return stats
    records = json.loads(config.BLUEBOOK_JSON.read_text())
    if isinstance(records, dict):
        records = list(records.values())
    uid_to_qid = _historical_uid_map(conn)
    for rec in records:
        _repair_record(conn, rec, stats, uid_to_qid)
    stats["duplicate_placements"] = _count_duplicate_placements(conn)
    return stats


def _merge_json_only(rec: dict) -> tuple[dict, list[str]]:
    """Merge path when no snapshot exists: JSON record only."""
    parsed = ParsedQuestion()
    parsed.passage = rec.get("question_text") or ""
    parsed.choices = _json_choices(rec, _status_of(rec)[1])
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


def _append_media_update(row, merged: dict, updates: list[tuple[str, str]]) -> None:
    new_images = json.dumps(merged["images"])
    if row["images_json"] in ("[]", "") and new_images not in ("[]",):
        updates.append(("images_json", new_images))


def _append_fingerprint_update(
    conn, row, qid: int, merged: dict, fp: str, updates: list[tuple[str, str]]
) -> None:
    if not merged["choices"] or fp == row["fingerprint"]:
        return
    owner = conn.execute(
        "SELECT id FROM questions WHERE fingerprint=? AND id!=?",
        (fp, qid),
    ).fetchone()
    if owner is None:
        updates.append(("fingerprint", fp))


def _reconcile_question(conn, qid: int, rec: dict, merged: dict, fp: str) -> bool:
    """Fill gaps on an existing historical row in place.

    Keeps the same question_id so attempts/tags/reviews stay attached; never
    re-keys by a fresh fingerprint (that would orphan the rows that reference
    this question). Returns True when any column actually changed.
    """
    row = conn.execute(
        "SELECT passage, stem, choices_json, correct_letter, rationale, images_json, provenance_json, fingerprint FROM questions WHERE id=?",
        (qid,),
    ).fetchone()
    if row is None:
        return False
    updates: list[tuple[str, str]] = []
    # T2: choices are only updated when the merged source actually supplies
    # them. If the snapshot is missing and the JSON record has none, keep the
    # stored choices rather than corrupting the row with an empty list.
    if merged["choices"] and json.dumps(merged["choices"]) != row["choices_json"]:
        updates.append(("choices_json", json.dumps(merged["choices"])))
    if not row["correct_letter"] and merged["correct_letter"]:
        updates.append(("correct_letter", merged["correct_letter"]))
    if not row["passage"] and merged["passage"]:
        updates.append(("passage", merged["passage"]))
    if not row["stem"] and merged["stem"]:
        updates.append(("stem", merged["stem"]))
    if not row["rationale"] and merged["rationale"]:
        updates.append(("rationale", merged["rationale"]))
    # Images follow the same fill-if-missing rule as the other fields: when
    # the row already has images, keep them. For content-collided rows (two
    # occurrences sharing one question with different figure paths, e.g. the
    # same item reused across modules) this keeps the write idempotent instead
    # of oscillating image paths on every run.
    _append_media_update(row, merged, updates)
    # T1: reconcile the content identity when the corrected fingerprint is
    # free AND the merged source actually recovered real content (choices
    # non-empty). Re-keying on a degenerate run (snapshot missing, JSON
    # empty) would overwrite a meaningful fingerprint with an empty-content
    # one. The row keeps its id (attempts/tags/reviews stay attached); only
    # update when no OTHER row already owns that exact fingerprint (that is
    # a genuine content collision handled by the occurrence table).
    _append_fingerprint_update(conn, row, qid, merged, fp, updates)
    if updates:
        sets = ", ".join(f"{col}=?" for col, _ in updates)
        conn.execute(
            f"UPDATE questions SET {sets} WHERE id=?",
            [val for _, val in updates] + [qid],
        )
    return bool(updates)


def _ensure_historical_attempt(conn, qid: int, rec: dict, merged: dict) -> None:
    """Insert the historical attempt and backfill error diagnosis (T9).

    Attempt insertion stays conditional (once per uid), but diagnosis is
    run whenever a wrong historical attempt can now be explained — the main
    repair case is a mobile wrong attempt whose original ingest could not
    call ``diagnose_attempt`` because the question had no choices.
    """
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
    # Backfill error diagnosis idempotently when the wrong letter and the
    # (now recovered) choice set are both known. diagnose_attempt replaces
    # prior tags of the same question, so re-running is safe.
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
