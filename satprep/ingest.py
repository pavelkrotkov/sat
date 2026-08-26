"""Ingestion: raw sources -> normalized SQLite corpus.

* Bluebook history (outputs/wrong_questions.json + artifacts/html snapshots)
  becomes pool='historical' questions plus one historical attempt each.
* Official College Board Question Bank exports (imports/*.csv|json) become
  fresh questions split deterministically into fresh_training vs
  protected_benchmark pools.

Idempotent: fingerprints are unique; rerunning never duplicates questions
or attempt history. Raw sources are only ever read.
"""

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config, fingerprint as fpmod
from .parse_snapshot import parse_snapshot


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _letter(value: str | None) -> str:
    """Extract an answer letter; labels like 'Correct Answer: B' yield B, not C."""
    if not value:
        return ""
    matches = re.findall(r"(?:^|[^A-Za-z])([A-H])(?:[^A-Za-z]|$)", str(value).upper())
    return matches[-1] if matches else ""


def _status_of(rec: dict) -> tuple[str, str]:
    """Return (student_letter, correct_letter) preferring snapshot data."""
    my = rec.get("my_answer") or ""
    student = _letter(my.split(";")[0]) if ";" in my else (_letter(my) if "correct" not in my.lower() else "")
    correct = _letter(rec.get("correct_answer"))
    return student, correct


def _historical_correctness(rec: dict) -> int | None:
    status = rec.get("answer_status")
    if status in ("Correct", "Incorrect"):
        return 1 if status == "Correct" else 0
    my = (rec.get("my_answer") or "").lower()
    if "incorrect" in my:
        return 0
    if "correct" in my and rec.get("correct_answer"):
        return 1
    return None  # unknown - do not fabricate


def ingest_bluebook(conn) -> dict:
    """Ingest the scraped 8-test history. Idempotent."""
    stats = {"records_seen": 0, "rw_records": 0, "questions_added": 0,
             "attempts_added": 0, "parse_fallbacks": 0, "skipped_existing": 0}
    if not config.BLUEBOOK_JSON.exists():
        return stats

    records = json.loads(config.BLUEBOOK_JSON.read_text())
    if isinstance(records, dict):
        records = list(records.values())

    for rec in records:
        stats["records_seen"] += 1
        if rec.get("subject_bucket") != config.SUBJECT:
            continue
        stats["rw_records"] += 1

        parsed = None
        snap_path = rec.get("html_snapshot_path")
        if snap_path and Path(snap_path).exists():
            try:
                parsed = parse_snapshot(Path(snap_path).read_text())
            except Exception:
                parsed = None
        if parsed is None or not (parsed.passage or parsed.stem):
            # snapshot truly unusable -> rebuild from scraped JSON text
            stats["parse_fallbacks"] += 1
            parsed_json = _question_from_json_record(rec)
            if parsed is None or not parsed.choices:
                parsed = parsed_json
            else:
                parsed.passage = parsed.passage or parsed_json.passage
                parsed.stem = parsed.stem or parsed_json.stem

        student_json, correct_json = _status_of(rec)
        correct_letter = parsed.correct_letter or correct_json
        if not correct_letter:
            continue  # cannot establish an answer key; refuse to fabricate
        student_letter = parsed.student_letter or student_json

        passage = parsed.passage or rec.get("question_text") or ""
        stem = parsed.stem or ""
        choice_texts = [c["text"] for c in parsed.choices]
        fp = fpmod.fingerprint(passage, stem, choice_texts)

        existing = conn.execute(
            "SELECT id FROM questions WHERE fingerprint=?", (fp,)
        ).fetchone()
        if existing:
            stats["skipped_existing"] += 1
            qid = existing["id"]
        else:
            provenance = {
                "bluebook_uid": rec.get("uid"),
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
                    fp, "bluebook_test", rec.get("test_name") or "", str(rec.get("question_number") or ""),
                    rec.get("module") or "",
                    passage, stem, json.dumps(parsed.choices), correct_letter,
                    parsed.rationale or rec.get("explanation") or "",
                    json.dumps(rec.get("images") or []),
                    rec.get("domain") or "", rec.get("skill") or "",
                    "metadata" if rec.get("skill") else "unknown",
                    "",  # difficulty unknown for bluebook history
                    "historical", utc_now(), json.dumps(provenance),
                ),
            )
            qid = cur.lastrowid
            stats["questions_added"] += 1

        # Historical attempt (once per uid even after dedupe/re-runs).
        correctness = _historical_correctness(rec)
        session_key = f"hist:{rec.get('uid')}"
        seen = conn.execute("SELECT 1 FROM attempts WHERE session_id=?", (session_key,)).fetchone()
        if correctness is not None and not seen:
            conn.execute(
                """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                         confidence, time_ms, mode, attempted_at)
                   VALUES (?,?,?,?,0,0,'historical',?)""",
                (session_key, qid, student_letter, correctness, rec.get("scraped_at") or utc_now()),
            )
            conn.execute(
                """INSERT INTO question_state (question_id, due_at) VALUES (?, NULL)
                   ON CONFLICT(question_id) DO NOTHING""",
                (qid,),
            )
            stats["attempts_added"] += 1

            # Historical error diagnosis (spec section 6): only when both the
            # chosen wrong letter and the full choice set are known. Never
            # fabricated from bare right/wrong.
            if correctness == 0 and student_letter and parsed.choices:
                from .tagger import diagnose_attempt

                diagnose_attempt(conn, qid, parsed.choices, correct_letter, student_letter)

    return stats


def _question_from_json_record(rec: dict) -> "object":
    from .parse_snapshot import ParsedQuestion

    text = rec.get("question_text") or ""
    choices = []
    for i, raw in enumerate(rec.get("answer_choices") or []):
        t = re.sub(r"^[A-H][\.\)]\s*", "", str(raw))
        choices.append({"letter": chr(ord("A") + i), "text": t.strip(), "is_correct": False})
    ca = _letter(rec.get("correct_answer"))
    for c in choices:
        c["is_correct"] = c["letter"] == ca
    return ParsedQuestion(
        section=rec.get("section") or "",
        question_number=str(rec.get("question_number") or ""),
        passage=text,
        stem="",
        choices=choices,
        correct_letter=ca,
        student_letter=_letter((rec.get("my_answer") or "").split(";")[0]),
        rationale=rec.get("explanation") or "",
    )


# --------------------------------------------------------------- qbank -----

def _qbank_rows_from_file(path: Path) -> list[dict]:
    """Normalize supported official export shapes into row dicts.

    Supported keys (case/space-insensitive): passage, stem/question, choices/
    answer_options (list of strings or {letter,text} dicts), correct/correct_
    answer/answer_key, domain, skill, difficulty, rationale/explanation,
    id/question_id.
    """
    rows: list[dict] = []
    suffix = path.suffix.lower()

    def norm_key(k: str) -> str:
        return re.sub(r"[^a-z]", "", k.lower())

    def get(d: dict, *names, default=""):
        keys = {norm_key(n) for n in names}
        for k, v in d.items():
            if norm_key(k) in keys:
                return v
        return default

    if suffix in (".json", ".ndjson"):
        raw = path.read_text()
        data = json.loads(raw) if not path.name.endswith(".ndjson") else [json.loads(l) for l in raw.splitlines() if l.strip()]
        if isinstance(data, dict):
            data = data.get("questions") or data.get("items") or [data]
        for item in data:
            rows.append({
                "passage": str(get(item, "passage", "stimulus", default="")),
                "stem": str(get(item, "stem", "question", "questiontext", default="")),
                "choices": get(item, "choices", "answeroptions", "options", default=[]),
                "correct": str(get(item, "correct", "correctanswer", "answerkey", "answer", default="")),
                "domain": str(get(item, "domain", "contentdomain", default="")),
                "skill": str(get(item, "skill", "skillknowledge", "testingpoint", default="")),
                "difficulty": str(get(item, "difficulty", "level", "hardness", default="")).lower(),
                "rationale": str(get(item, "rationale", "explanation", default="")),
                "ext_id": str(get(item, "id", "questionid", default="")),
            })
    elif suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rows.append({
                    "passage": str(get(row, "passage", "stimulus", default="")),
                    "stem": str(get(row, "stem", "question", default="")),
                    "choices": [v for k, v in row.items() if norm_key(k).startswith("choice") and v],
                    "correct": str(get(row, "correct", "correctanswer", "answerkey", "answer", default="")),
                    "domain": str(get(row, "domain", default="")),
                    "skill": str(get(row, "skill", default="")),
                    "difficulty": str(get(row, "difficulty", "level", default="")).lower(),
                    "rationale": str(get(row, "rationale", "explanation", default="")),
                    "ext_id": str(get(row, "id", "questionid", default="")),
                })
    else:
        raise ValueError(f"Unsupported Question Bank format: {path.name} "
                         f"(export CSV or JSON from the College Board Question Bank)")
    return rows


def _normalize_qbank_choices(raw) -> list[dict]:
    choices = []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [p.strip() for p in re.split(r";|\|", raw) if p.strip()]
    if isinstance(raw, dict):
        raw = [{"letter": k, "text": v} for k, v in raw.items()]
    for i, item in enumerate(raw or []):
        if isinstance(item, dict):
            letter = _letter(str(item.get("letter") or chr(ord("A") + i)))
            text = str(item.get("text") or "")
        else:
            m = re.match(r"^\s*\(?([A-H])[\.\)]\s*(.*)$", str(item))
            letter = m.group(1) if m else chr(ord("A") + i)
            text = m.group(2) if m else str(item)
        choices.append({"letter": letter, "text": text.strip(), "is_correct": False})
    return choices


def ingest_qbank(conn, path_or_dir=None, batch_name: str | None = None) -> dict:
    """Ingest official College Board Question Bank exports from imports/.

    Every file is treated as its own batch (batch defaults to the file name).
    Fingerprints are matched against ALL existing questions so bank items
    duplicated from the eight practice tests are not counted as fresh.
    """
    base = Path(path_or_dir) if path_or_dir else config.IMPORT_DIR
    base.mkdir(parents=True, exist_ok=True)
    files = [p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix.lower() in (".json", ".ndjson", ".csv")]
    stats = {"files": len(files), "rows_seen": 0, "added_fresh": 0, "duplicates": 0, "invalid": 0}

    from .qbank_fetch import insert_qbank_row

    for path in files:
        batch = batch_name or path.stem
        try:
            rows = _qbank_rows_from_file(path)
        except Exception as exc:
            print(f"[warn] could not parse {path.name}: {exc}")
            continue
        for row in rows:
            stats["rows_seen"] += 1
            choices = _normalize_qbank_choices(row["choices"])
            correct = _letter(row["correct"])
            if len(choices) < 2 or not correct:
                stats["invalid"] += 1
                continue
            for c in choices:
                c["is_correct"] = c["letter"] == correct
            row["choices"] = choices
            row["correct"] = correct
            row["_provenance"] = {"import_file": str(path), "external_id": row.get("ext_id", "")}
            outcome = insert_qbank_row(conn, row, batch)
            if outcome == "added":
                stats["added_fresh"] += 1
            elif outcome == "duplicate":
                stats["duplicates"] += 1
    return stats


def mark_benchmark_seen(conn, question_ids: list[int]) -> None:
    """After a Fresh Benchmark answer, move items into the training pool."""
    for qid in question_ids:
        conn.execute(
            """UPDATE questions SET seen_benchmark=1, pool='fresh_training'
               WHERE id=? AND pool='protected_benchmark'""",
            (qid,),
        )


if __name__ == "__main__":
    from .db import db_context

    with db_context() as _conn:
        print(ingest_bluebook(_conn))
