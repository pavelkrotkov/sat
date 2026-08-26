"""Corpus archive: versioned JSONL snapshot + restore (spec section 24).

exports/corpus-v1.jsonl is the photocopy set: one self-contained question per
line (content + tags + provenance, images by reference - never attempt state).
Written atomically after every ingest/fetch; restore rebuilds a fresh database
from the file alone, without artifacts/ or network access.
"""

import json
from pathlib import Path

from ..clock import utc_now

from .. import config
from .tags import all_tags_with_origin, restore_tag

ARCHIVE_VERSION = 1


def _question_line(conn, row) -> dict:
    # The archive is faithful: suppressions are decisions worth preserving.
    tags = [{"tag": t, "origin": o} for t, o in all_tags_with_origin(conn, row["id"])]
    return {
        "_v": ARCHIVE_VERSION,
        "fingerprint": row["fingerprint"],
        "source": row["source"],
        "source_test": row["source_test"],
        "source_question_number": row["source_question_number"],
        "module": row["module"],
        "passage": row["passage"],
        "stem": row["stem"],
        "choices": json.loads(row["choices_json"]),
        "correct_letter": row["correct_letter"],
        "rationale": row["rationale"],
        "images": json.loads(row["images_json"] or "[]"),
        "official_domain": row["official_domain"],
        "official_skill": row["official_skill"],
        "skill_source": row["skill_source"],
        "difficulty": row["difficulty"],
        "pool": row["pool"],
        "is_new_bank": row["is_new_bank"],
        "import_batch": row["import_batch"],
        "provenance": json.loads(row["provenance_json"] or "{}"),
        "tags": tags,
    }


def export_corpus(conn, out_path: Path | None = None) -> Path:
    """Atomically rewrite the JSONL archive from the live corpus.

    Guard: an empty corpus must never overwrite an existing non-empty
    archive - that archive may be the only surviving copy. A missing
    database now reaches this as an empty corpus, since the caller opened
    the connection; `cli.cmd_export` still checks the file up front so it
    can point at `satprep restore` instead.
    """
    out_path = Path(out_path) if out_path else config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if conn.execute("SELECT COUNT(*) FROM questions WHERE active=1").fetchone()[0] == 0 \
            and out_path.exists() and out_path.stat().st_size > 0:
        raise RuntimeError(
            f"Live corpus is empty; refusing to replace non-empty archive {out_path}."
        )
    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in conn.execute("SELECT * FROM questions WHERE active=1 ORDER BY id"):
            fh.write(json.dumps(_question_line(conn, row), ensure_ascii=False) + "\n")
    tmp.replace(out_path)
    return out_path


def restore_corpus(conn, archive_path: Path | None = None) -> dict:
    """Rebuild question content + tags from a JSONL archive. Idempotent.

    Training state (attempts, sessions, weakness cache) is intentionally NOT
    restored - it lives only in data/satprep.db backups. A malformed line
    aborts the whole restore; `db_context` rolls the caller's transaction
    back, so a half-applied archive is never left behind.
    """
    archive_path = Path(archive_path) if archive_path else config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl"
    if not archive_path.exists():
        raise FileNotFoundError(f"No archive at {archive_path}")
    stats = {"lines": 0, "restored": 0, "duplicates": 0, "invalid": 0}
    _restore_lines(conn, archive_path, stats)
    return stats


def _restore_lines(conn, archive_path: Path, stats: dict) -> None:
    head = next((l for l in archive_path.read_text(encoding="utf-8").splitlines() if l.strip()), "")
    if head:
        v = json.loads(head).get("_v")
        if v != ARCHIVE_VERSION:
            raise ValueError(
                f"Archive schema v{v} unsupported by this build (expects v{ARCHIVE_VERSION}); "
                f"upgrade satprep or use the matching release."
            )
    for line_no, line in enumerate(archive_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        stats["lines"] += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{archive_path.name}:{line_no}: malformed JSON ({exc})") from None
        if not isinstance(rec, dict) or not rec.get("fingerprint") or not rec.get("correct_letter"):
            stats["invalid"] += 1
            continue
        # A restore must be FAITHFUL: keep the archived fingerprint verbatim
        # (recomputing would break reconciled rows whose content changed).
        cur = conn.execute(
            """INSERT OR IGNORE INTO questions
               (fingerprint, source, source_test, source_question_number, module,
                passage, stem, choices_json, correct_letter, rationale, images_json,
                official_domain, official_skill, skill_source, difficulty,
                pool, seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
            (
                rec["fingerprint"], rec.get("source", ""), rec.get("source_test", ""),
                str(rec.get("source_question_number", "")), rec.get("module", ""),
                rec.get("passage", ""), rec.get("stem", ""),
                json.dumps(rec.get("choices", [])), rec["correct_letter"],
                rec.get("rationale", ""), json.dumps(rec.get("images", [])),
                rec.get("official_domain", ""), rec.get("official_skill", ""),
                rec.get("skill_source") or ("archive" if rec.get("official_skill") else "unknown"),
                rec.get("difficulty", ""), rec.get("pool", "historical"),
                int(rec.get("is_new_bank", 0)), rec.get("import_batch", ""),
                utc_now(), json.dumps(rec.get("provenance", {})),
            ),
        )
        if cur.rowcount == 0:
            stats["duplicates"] += 1
            continue
        qid = cur.lastrowid
        conn.execute("INSERT INTO question_state (question_id) VALUES (?)", (qid,))
        for t in rec.get("tags", []):
            if isinstance(t, dict):
                tag, origin = t["tag"], t.get("origin", "archive")
            else:
                tag, origin = t, "archive"
            restore_tag(conn, qid, tag, origin)
        stats["restored"] += 1
