"""Corpus audit for Bluebook history ingestion (issue #49).

Verifies the acceptance criteria are met from live DB state, using the
source scrape (``outputs/wrong_questions.json``) as ground truth:

* every source R&W occurrence (test/module/question + bluebook_uid) has a
  ``bluebook_occurrences`` row resolving to a question;
* every resolved question has passage, stem, choices, key and rationale —
  any genuinely missing field is reported;
* historical attempts/tags remain attached after repair;
* duplicate placements (two scraped reviews for one test/module/question,
  e.g. an empty-status record and an Incorrect record) are reported
  deliberately rather than silently collapsing.

Run via ``satprep audit-bluebook``; exits non-zero when the corpus fails
an acceptance gate. The report is JSON so it can drive CI or a human
review pass.
"""

from __future__ import annotations

import json

from .. import config
from .questions import Question


def _has_determinable_correctness(rec: dict) -> bool:
    """True when a source record should have produced a historical attempt."""
    status = rec.get("answer_status")
    if status in ("Correct", "Incorrect"):
        return True
    my = (rec.get("my_answer") or "").lower()
    return bool("incorrect" in my or ("correct" in my and rec.get("correct_answer")))


def _source_records() -> list[dict]:
    """The scraped R&W records: the ground-truth occurrence set."""
    if not config.BLUEBOOK_JSON.exists():
        return []
    records = json.loads(config.BLUEBOOK_JSON.read_text())
    if isinstance(records, dict):
        records = list(records.values())
    return [r for r in records if r.get("subject_bucket") == config.SUBJECT]


def _audit_occurrences(conn, report: dict) -> tuple[list, dict]:
    rows = conn.execute(
        """SELECT bluebook_uid, test_name, module, question_number,
                  question_id
           FROM bluebook_occurrences
           ORDER BY test_name, module, question_number"""
    ).fetchall()
    report["occurrences_total"] = len(rows)
    report["occurrences_with_question"] = sum(1 for row in rows if row["question_id"] is not None)
    return rows, {row["bluebook_uid"]: row for row in rows}


def _audit_source_occurrences(report: dict, sources: list[dict], occ_by_uid: dict) -> None:
    report["source_records"] = len(sources)
    if not sources:
        report["failures"].append("source scrape has zero R&W records; corpus is unauditable")
    placements: dict[tuple, list[str]] = {}
    for rec in sources:
        uid = rec.get("uid") or ""
        placement = (
            rec.get("test_name") or "",
            rec.get("module") or "",
            str(rec.get("question_number") or ""),
        )
        placements.setdefault(placement, []).append(uid)
        if uid not in occ_by_uid:
            report["missing_occurrences"].append(
                {"bluebook_uid": uid, "placement": list(placement)}
            )
    for placement, uids in sorted(placements.items()):
        if len(uids) > 1:
            report["duplicate_placements"].append(
                {
                    "test": placement[0],
                    "module": placement[1],
                    "question": placement[2],
                    "count": len(uids),
                    "uids": uids,
                }
            )


def _audit_question_gaps(conn, report: dict, rows: list) -> None:
    qids = {row["question_id"] for row in rows if row["question_id"] is not None}
    for qid in sorted(qids):
        row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if row is None:
            report["question_gaps"].append({"question_id": qid, "missing": ["question row"]})
            continue
        question = Question.from_row(row)
        missing = [
            name
            for name, value in (
                ("passage", question.passage),
                ("stem", question.stem),
                ("choices", question.choices),
                ("correct_letter", question.correct_letter),
                ("rationale", question.rationale),
            )
            if not value
        ]
        if missing:
            report["question_gaps"].append({"question_id": qid, "missing": missing})


def _audit_attempts(conn, report: dict, sources: list[dict], occ_by_uid: dict) -> None:
    report["attempts_preserved"] = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE mode='historical'"
    ).fetchone()[0]
    for rec in sources:
        uid = rec.get("uid") or ""
        occ = occ_by_uid.get(uid)
        if occ is None or occ["question_id"] is None:
            continue
        row = conn.execute(
            "SELECT question_id FROM attempts WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (f"hist:{uid}",),
        ).fetchone()
        if row is None:
            if _has_determinable_correctness(rec):
                report["attempts_missing"].append(uid)
        elif row["question_id"] != occ["question_id"]:
            report["attempts_misplaced"].append(
                {
                    "bluebook_uid": uid,
                    "expected_qid": occ["question_id"],
                    "actual_qid": row["question_id"],
                }
            )


def _audit_failures(report: dict) -> None:
    if report["missing_occurrences"]:
        report["failures"].append(
            f"{len(report['missing_occurrences'])} source occurrence(s) have no "
            "bluebook_occurrences row"
        )
    if report["question_gaps"]:
        report["failures"].append(f"{len(report['question_gaps'])} question(s) have missing fields")
    if report["occurrences_with_question"] < report["occurrences_total"]:
        report["failures"].append("some occurrences resolve to no question")
    if report["attempts_missing"]:
        report["failures"].append(
            f"{len(report['attempts_missing'])} source occurrence(s) are missing "
            "their historical attempt"
        )
    if report["attempts_misplaced"]:
        report["failures"].append(
            f"{len(report['attempts_misplaced'])} historical attempt(s) attached "
            "to the wrong question"
        )


def audit_bluebook(conn) -> dict:
    """Audit the repaired historical corpus against the source scrape."""
    report: dict = {
        "occurrences_total": 0,
        "occurrences_with_question": 0,
        "source_records": 0,
        "missing_occurrences": [],
        "duplicate_placements": [],
        "question_gaps": [],
        "attempts_preserved": 0,
        "attempts_missing": [],
        "attempts_misplaced": [],
        "failures": [],
    }
    if not config.BLUEBOOK_JSON.exists():
        report["failures"].append(f"source scrape missing at {config.BLUEBOOK_JSON}; cannot audit")
        return report
    rows, occ_by_uid = _audit_occurrences(conn, report)
    sources = _source_records()
    _audit_source_occurrences(report, sources, occ_by_uid)
    _audit_question_gaps(conn, report, rows)
    _audit_attempts(conn, report, sources, occ_by_uid)
    _audit_failures(report)
    return report


def audit_passes(report: dict) -> bool:
    """True when every acceptance gate holds.

    Duplicate placements are reported but do not fail the audit: they are
    deliberate double-scrapes whose provenance is preserved (the issue asks
    for them to be flagged, not erased).
    """
    return not report["failures"]
