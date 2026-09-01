"""Corpus archive: versioned JSONL snapshot + restore (spec section 24).

exports/corpus-v1.jsonl is the photocopy set: one self-contained question per
line (content + tags + provenance, images by reference - never attempt state).
Written atomically after every ingest/fetch; restore rebuilds a fresh database
from the file alone, without artifacts/ or network access.
"""

import json
from pathlib import Path

from .. import config
from ..clock import utc_now
from .qbank_fetch import sanitize_table
from .questions import Question
from .tags import all_tags_with_origin, restore_tag

ARCHIVE_VERSION = 2
#: Old snapshots (pre-visuals) carry _v: 1 and have no `visuals` field.
#: Restores must accept them: their questions simply read with empty
#: visuals, and a later visual backfill can attach records. The version
#: guard exists to refuse FUTURE/incompatible snapshots, not historical
#: ones that are a strict subset.
LEGACY_ARCHIVE_VERSIONS = {1}


def _question_line(conn, question: Question) -> dict:
    # The archive is faithful: suppressions are decisions worth preserving.
    tags = [{"tag": t, "origin": o} for t, o in all_tags_with_origin(conn, question.id)]
    line = {
        "_v": ARCHIVE_VERSION,
        "fingerprint": question.fingerprint,
        "source": question.source,
        "source_test": question.source_test,
        "source_question_number": question.source_question_number,
        "module": question.module,
        "passage": question.passage,
        "stem": question.stem,
        "choices": [c.as_dict() for c in question.choices],
        "correct_letter": question.correct_letter,
        "rationale": question.rationale,
        "images": list(question.images),
        "visuals": [dict(v) for v in question.visuals],
        "official_domain": question.official_domain,
        "official_skill": question.official_skill,
        "skill_source": question.skill_source,
        "difficulty": question.difficulty,
        "pool": question.pool,
        "is_new_bank": question.is_new_bank,
        "import_batch": question.import_batch,
        "provenance": question.provenance,
        "tags": tags,
    }
    # T4: bluebook_occurrences carry source UIDs and placements that can be
    # the ONLY record of deduplicated occurrences; without them a restore
    # silently drops source identity. Serialize them per question so the
    # standalone archive stays faithful. Optional field: v1 archives without
    # it still restore (occurrences are reconstructible from provenance when
    # they carry fresh fingerprints, but deduplicated ones are not).
    if question.source == "bluebook_test":
        occ = conn.execute(
            """SELECT bluebook_uid, test_name, module, question_number, subject,
                      fingerprint, answer_status, scraped_at
               FROM bluebook_occurrences WHERE question_id=?
               ORDER BY bluebook_uid""",
            (question.id,),
        ).fetchall()
        if occ:
            line["occurrences"] = [dict(r) for r in occ]
    return line


_RESTORE_VISUAL_KINDS = {"image", "table"}


def _restore_visuals(raw) -> list[dict]:
    """Validate/sanitize visual records from an archive before persisting.

    Round-3 finding: a crafted or modified v2 archive can carry arbitrary
    markup in visuals[].html, which the template renders with |safe. Table
    records are re-run through `sanitize_table` (the same allowlist the
    live ingest uses) so no script/event/URL content survives a restore;
    image records are reduced to their filename; anything else is dropped.
    """
    out = []
    for v in raw if isinstance(raw, list) else []:
        if not isinstance(v, dict) or v.get("kind") not in _RESTORE_VISUAL_KINDS:
            continue
        if v["kind"] == "image":
            fname = v.get("file")
            if isinstance(fname, str) and fname:
                out.append({"kind": "image", "file": fname})
            continue
        # table: re-sanitize — never trust archive html verbatim
        html = v.get("html")
        if isinstance(html, str):
            safe = sanitize_table(html)
            if safe is not None:
                out.append({"kind": "table", "html": safe})
    return out


def export_corpus(conn, out_path: Path | None = None) -> Path:
    """Atomically rewrite the JSONL archive from the live corpus.

    Guard: an empty corpus must never overwrite an existing non-empty
    archive - that archive may be the only surviving copy. A missing
    database now reaches this as an empty corpus, since the caller opened
    the connection; `cli.cmd_export` still checks the file up front so it
    can point at `satprep restore` instead.
    """
    out_path = (
        Path(out_path)
        if out_path
        else config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if (
        conn.execute("SELECT COUNT(*) FROM questions WHERE active=1").fetchone()[0] == 0
        and out_path.exists()
        and out_path.stat().st_size > 0
    ):
        raise RuntimeError(
            f"Live corpus is empty; refusing to replace non-empty archive {out_path}."
        )
    tmp = out_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in conn.execute("SELECT * FROM questions WHERE active=1 ORDER BY id"):
            fh.write(
                json.dumps(_question_line(conn, Question.from_row(row)), ensure_ascii=False) + "\n"
            )
    tmp.replace(out_path)
    return out_path


def restore_corpus(conn, archive_path: Path | None = None) -> dict:
    """Rebuild question content + tags from a JSONL archive. Idempotent.

    Training state (attempts, sessions, weakness cache) is intentionally NOT
    restored - it lives only in data/satprep.db backups. A malformed line
    aborts the whole restore; `db_context` rolls the caller's transaction
    back, so a half-applied archive is never left behind.
    """
    archive_path = (
        Path(archive_path)
        if archive_path
        else config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl"
    )
    if not archive_path.exists():
        # Round-3 finding: an upgraded host has only the pre-visuals v1
        # archive file; the default path must fall back to it so `satprep
        # restore` keeps working after the v1->v2 bump.
        legacy = config.REPO_ROOT / "exports" / f"corpus-v{min(LEGACY_ARCHIVE_VERSIONS)}.jsonl"
        if legacy.exists():
            archive_path = legacy
        else:
            raise FileNotFoundError(f"No archive at {archive_path}")
    stats = {"lines": 0, "restored": 0, "duplicates": 0, "invalid": 0}
    _restore_lines(conn, archive_path, stats)
    return stats


def _restore_lines(conn, archive_path: Path, stats: dict) -> None:
    head = next(
        (line for line in archive_path.read_text(encoding="utf-8").splitlines() if line.strip()),
        "",
    )
    if head:
        v = json.loads(head).get("_v")
        if v not in (ARCHIVE_VERSION, *LEGACY_ARCHIVE_VERSIONS):
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
        visuals = _restore_visuals(rec.get("visuals", []))
        cur = conn.execute(
            """INSERT OR IGNORE INTO questions
              (fingerprint, source, source_test, source_question_number, module,
               passage, stem, choices_json, correct_letter, rationale, images_json,
               visuals_json,
               official_domain, official_skill, skill_source, difficulty,
               pool, seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
            (
                rec["fingerprint"],
                rec.get("source", ""),
                rec.get("source_test", ""),
                str(rec.get("source_question_number", "")),
                rec.get("module", ""),
                rec.get("passage", ""),
                rec.get("stem", ""),
                json.dumps(rec.get("choices", [])),
                rec["correct_letter"],
                rec.get("rationale", ""),
                json.dumps(rec.get("images", [])),
                json.dumps(visuals),
                rec.get("official_domain", ""),
                rec.get("official_skill", ""),
                rec.get("skill_source") or ("archive" if rec.get("official_skill") else "unknown"),
                rec.get("difficulty", ""),
                rec.get("pool", "historical"),
                int(rec.get("is_new_bank", 0)),
                rec.get("import_batch", ""),
                utc_now(),
                json.dumps(rec.get("provenance", {})),
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
        # T4: restore source occurrences faithfully when the archive carried
        # them. The question_id is re-linked to the freshly restored row.
        for occ in rec.get("occurrences", []):
            if not isinstance(occ, dict) or not occ.get("bluebook_uid"):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO bluebook_occurrences
                     (bluebook_uid, test_name, module, question_number, subject,
                      fingerprint, question_id, answer_status, scraped_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    occ["bluebook_uid"],
                    occ.get("test_name") or "",
                    occ.get("module") or "",
                    str(occ.get("question_number") or ""),
                    occ.get("subject") or "",
                    occ.get("fingerprint") or "",
                    qid,
                    occ.get("answer_status") or "",
                    occ.get("scraped_at") or "",
                ),
            )
        stats["restored"] += 1
