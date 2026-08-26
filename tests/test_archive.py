import argparse
import json
from pathlib import Path

from satprep.archive import export_corpus, restore_corpus
from satprep.db import connect, db_context
from conftest import add_question


def _restore(db_file, archive):
    """Restore into a fresh database, committing as db_context would."""
    conn = connect(str(db_file))
    try:
        stats = restore_corpus(conn, archive)
        conn.commit()
        return stats
    finally:
        conn.close()


def test_round_trip_preserves_content_and_tags(db, tmp_path):
    conn, path = db
    qid = add_question(conn, passage="Passage one", stem="Stem?",
                       choices=["a", "b", "c", "d"], correct="B",
                       source="bluebook_test", pool="historical",
                       difficulty="hard", skill="Inferences", tags=("qualifier_strength",))
    conn.execute("UPDATE questions SET images_json='[\"artifacts/images/fig.svg\"]' WHERE id=?", (qid,))
    conn.commit()
    out = Path(tmp_path) / "corpus.jsonl"
    export_corpus(conn, out)
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["correct_letter"] == "B"
    assert rec["tags"] == [{"tag": "qualifier_strength", "origin": "rule"}]
    assert rec["images"] == ["artifacts/images/fig.svg"]

    # restore into a brand-new database
    fresh = Path(tmp_path) / "fresh.db"
    stats = _restore(fresh, out)
    assert stats["restored"] == 1 and stats["duplicates"] == 0
    c2 = connect(str(fresh))
    row = c2.execute("SELECT * FROM questions").fetchone()
    assert row["fingerprint"] == rec["fingerprint"]
    assert row["pool"] == "historical"          # not re-split as if unseen
    assert json.loads(row["choices_json"])[1]["text"] == "b"
    tags = {(r["tag"], r["origin"]) for r in c2.execute("SELECT tag, origin FROM question_tags")}
    c2.close()
    assert ("qualifier_strength", "rule") in tags


def test_restore_is_idempotent(db, tmp_path):
    conn, path = db
    add_question(conn)
    conn.commit()
    out = Path(tmp_path) / "corpus.jsonl"
    export_corpus(conn, out)
    fresh = Path(tmp_path) / "fresh.db"
    s1 = _restore(fresh, out)
    s2 = _restore(fresh, out)
    assert s1["restored"] == 1
    assert s2["restored"] == 0 and s2["duplicates"] == 1
    c2 = connect(str(fresh))
    n = c2.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    c2.close()
    assert n == 1


def test_export_excludes_training_state(db, tmp_path):
    """The archive is content-only: attempts/sessions never appear."""
    conn, path = db
    add_question(conn)
    conn.commit()
    out = Path(tmp_path) / "corpus.jsonl"
    export_corpus(conn, out)
    text = out.read_text()
    assert "attempts" not in text and "due_at" not in text


def test_suppressed_tag_survives_round_trip(db, tmp_path):
    """Codex P2: removing a rule tag must stay removed after restore + retag."""
    conn, path = db
    add_question(conn, tags=("qualifier_strength",))
    from satprep.ingest import utc_now
    conn.execute(
        "INSERT OR REPLACE INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,'suppressed',?)",
        (1, "qualifier_strength", utc_now()),
    )
    conn.commit()
    out = Path(tmp_path) / "c.jsonl"
    export_corpus(conn, out)
    fresh = Path(tmp_path) / "f.db"
    _restore(fresh, out)
    # simulate a full tagging run: rule re-insert is ignored by tombstone
    c2 = connect(str(fresh))
    c2.execute("INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (1,'qualifier_strength','rule','')")
    rows = [(r["tag"], r["origin"]) for r in c2.execute("SELECT tag, origin FROM question_tags")]
    c2.close()
    assert ("qualifier_strength", "suppressed") in rows
    assert ("qualifier_strength", "rule") not in rows


def test_export_refuses_to_clobber_archive_from_missing_db(db, tmp_path, monkeypatch):
    """export_corpus now receives an open connection, so it cannot tell a
    missing database from an empty one - it raises RuntimeError either way.
    The friendlier "run satprep restore" message lives in the CLI, which
    still has the path."""
    import pytest

    from satprep import cli, config

    out = Path(tmp_path) / "keep.jsonl"
    out.write_text('{"_v": 1}\n')

    with pytest.raises(RuntimeError):
        export_corpus(connect(tmp_path / "nonexistent.db"), out)
    assert out.read_text() == '{"_v": 1}\n'   # archive untouched

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "also-missing.db")
    with pytest.raises(SystemExit, match="satprep restore"):
        cli.cmd_export(argparse.Namespace(out=None))


def test_export_refuses_empty_live_corpus_over_nonempty_archive(db, tmp_path):
    conn, path = db
    out = Path(tmp_path) / "existing.jsonl"
    out.write_text('{"_v": 1}\n')
    import pytest
    with pytest.raises(RuntimeError):
        export_corpus(conn, out)      # live corpus empty here
    assert out.read_text() == '{"_v": 1}\n'


def test_restore_rejects_unsupported_version(db, tmp_path):
    conn, path = db
    add_question(conn); conn.commit()
    out = Path(tmp_path) / "future.jsonl"
    lines = export_corpus(conn, out).read_text().splitlines()
    rec = json.loads(lines[0]); rec["_v"] = 99
    future = Path(tmp_path) / "future.jsonl"
    future.write_text(json.dumps(rec) + "\n")
    import pytest
    with pytest.raises(ValueError, match="unsupported"):
        _restore(Path(tmp_path) / "x.db", future)


def test_export_with_custom_out_still_requires_a_database(tmp_path, monkeypatch):
    """Regression: the guard was `not args.out and not DB_PATH.exists()`, so
    the documented `satprep export --out path.jsonl` form skipped it entirely.
    db_context then created an empty database and the command reported a
    successful export of nothing."""
    import pytest

    from satprep import cli, config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "missing.db")
    out = Path(tmp_path) / "custom.jsonl"

    with pytest.raises(SystemExit, match="satprep restore"):
        cli.cmd_export(argparse.Namespace(out=str(out)))

    assert not out.exists()
    assert not (tmp_path / "missing.db").exists()   # no empty database created


def test_restore_validates_the_archive_before_creating_a_database(tmp_path, monkeypatch):
    """Regression: db_context created the database before restore_corpus
    checked the archive, so a mistyped path left an empty database behind -
    which then satisfied cmd_export's missing-database guard."""
    import pytest

    from satprep import cli, config

    db_file = tmp_path / "new.db"
    monkeypatch.setattr(config, "DB_PATH", db_file)

    with pytest.raises(SystemExit, match="No archive at"):
        cli.cmd_restore(argparse.Namespace(file=str(tmp_path / "typo.jsonl")))

    assert not db_file.exists()


def test_auto_export_publishes_only_committed_state(db, tmp_path, monkeypatch):
    """The archive is the only copy of question content, so it must never
    hold rows the database later rolls back."""
    import pytest

    from satprep import cli

    conn, path = db
    out = Path(tmp_path) / "corpus.jsonl"
    monkeypatch.setattr(cli, "_auto_export",
                        lambda c: (c.commit(), export_corpus(c, out))[1])

    with pytest.raises(RuntimeError, match="later failure"):
        with db_context(path) as tx:
            add_question(tx, passage="committed", stem="s?",
                         choices=["a", "b", "c", "d"])
            cli._auto_export(tx)
            add_question(tx, passage="rolled back", stem="r?",
                         choices=["a", "b", "c", "d"])
            raise RuntimeError("later failure")

    archived = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["passage"] for r in archived] == ["committed"]
