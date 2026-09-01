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


def audit_bluebook(conn) -> dict:
    """Audit the repaired historical corpus against the source scrape.

    Never writes. Every key is JSON-serialisable for the CLI report.
    """
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

    # T5: the source scrape is the ground truth for "expected". A missing
    # file or zero R&W records means we cannot audit, and must not report a
    # clean PASS on an empty/missing dataset.
    if not config.BLUEBOOK_JSON.exists():
        report["failures"].append(f"source scrape missing at {config.BLUEBOOK_JSON}; cannot audit")
        return report

    occ_rows = conn.execute(
        """SELECT bluebook_uid, test_name, module, question_number,
                  question_id
           FROM bluebook_occurrences
           ORDER BY test_name, module, question_number"""
    ).fetchall()
    report["occurrences_total"] = len(occ_rows)
    occ_by_uid = {r["bluebook_uid"]: r for r in occ_rows}
    report["occurrences_with_question"] = sum(1 for r in occ_rows if r["question_id"] is not None)

    # ---- every source occurrence must be present ----------------------
    sources = _source_records()
    report["source_records"] = len(sources)
    if report["source_records"] == 0:
        report["failures"].append("source scrape has zero R&W records; corpus is unauditable")
    seen_placements: dict[tuple, list[str]] = {}
    for rec in sources:
        uid = rec.get("uid") or ""
        placement = (
            rec.get("test_name") or "",
            rec.get("module") or "",
            str(rec.get("question_number") or ""),
        )
        seen_placements.setdefault(placement, []).append(uid)
        if uid not in occ_by_uid:
            report["missing_occurrences"].append(
                {
                    "bluebook_uid": uid,
                    "placement": list(placement),
                }
            )

    # ---- duplicate placements (intentional double-scrapes) ------------
    for placement, uids in sorted(seen_placements.items()):
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

    # ---- per-question field gaps --------------------------------------
    qids = {r["question_id"] for r in occ_rows if r["question_id"] is not None}
    for qid in sorted(qids):
        row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if row is None:
            report["question_gaps"].append({"question_id": qid, "missing": ["question row"]})
            continue
        q = Question.from_row(row)
        missing = []
        if not q.passage:
            missing.append("passage")
        if not q.stem:
            missing.append("stem")
        if not q.choices:
            missing.append("choices")
        if not q.correct_letter:
            missing.append("correct_letter")
        if not q.rationale:
            missing.append("rationale")
        if missing:
            report["question_gaps"].append({"question_id": qid, "missing": missing})

    # ---- attempts preserved (T3) ----------------------------------------
    report["attempts_preserved"] = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE mode='historical'"
    ).fetchone()[0]
    # A global count cannot catch a lost or misattached hist:<uid> attempt,
    # so compare each source occurrence against its resolved attempt. Every
    # scraped review with a determinable correctness should have exactly one
    # historical attempt attached to the occurrence's question.
    for rec in sources:
        uid = rec.get("uid") or ""
        occ = occ_by_uid.get(uid)
        if occ is None or occ["question_id"] is None:
            continue
        session_key = f"hist:{uid}"
        row = conn.execute(
            "SELECT question_id FROM attempts WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_key,),
        ).fetchone()
        if row is None:
            # Only flag records that should carry an attempt (determinable
            # correctness); unknown-correctness records were never given one.
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

    # ---- failures ------------------------------------------------------
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
    return report


def audit_passes(report: dict) -> bool:
    """True when every acceptance gate holds.

    Duplicate placements are reported but do not fail the audit: they are
    deliberate double-scrapes whose provenance is preserved (the issue asks
    for them to be flagged, not erased).
    """
    return not report["failures"]
