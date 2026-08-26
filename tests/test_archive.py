import json
from pathlib import Path

from satprep.archive import export_corpus, restore_corpus
from satprep.db import connect
from conftest import add_question


def test_round_trip_preserves_content_and_tags(db, tmp_path):
    conn, path = db
    qid = add_question(conn, passage="Passage one", stem="Stem?",
                       choices=["a", "b", "c", "d"], correct="B",
                       source="bluebook_test", pool="historical",
                       difficulty="hard", skill="Inferences", tags=("qualifier_strength",))
    conn.execute("UPDATE questions SET images_json='[\"artifacts/images/fig.svg\"]' WHERE id=?", (qid,))
    conn.commit()
    out = Path(tmp_path) / "corpus.jsonl"
    export_corpus(out, db_path=path)
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["correct_letter"] == "B"
    assert rec["tags"] == [{"tag": "qualifier_strength", "origin": "rule"}]
    assert rec["images"] == ["artifacts/images/fig.svg"]

    # restore into a brand-new database
    fresh = Path(tmp_path) / "fresh.db"
    stats = restore_corpus(out, db_path=str(fresh))
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
    export_corpus(out, db_path=path)
    fresh = Path(tmp_path) / "fresh.db"
    s1 = restore_corpus(out, db_path=str(fresh))
    s2 = restore_corpus(out, db_path=str(fresh))
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
    export_corpus(out, db_path=path)
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
    export_corpus(out, db_path=path)
    fresh = Path(tmp_path) / "f.db"
    restore_corpus(out, db_path=str(fresh))
    # simulate a full tagging run: rule re-insert is ignored by tombstone
    c2 = connect(str(fresh))
    c2.execute("INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (1,'qualifier_strength','rule','')")
    rows = [(r["tag"], r["origin"]) for r in c2.execute("SELECT tag, origin FROM question_tags")]
    c2.close()
    assert ("qualifier_strength", "suppressed") in rows
    assert ("qualifier_strength", "rule") not in rows


def test_export_refuses_to_clobber_archive_from_missing_db(db, tmp_path):
    out = Path(tmp_path) / "keep.jsonl"
    out.write_text('{"_v": 1}\n')
    import pytest
    with pytest.raises(FileNotFoundError):
        export_corpus(out, db_path=str(tmp_path / "nonexistent.db"))
    assert out.read_text() == '{"_v": 1}\n'   # archive untouched


def test_export_refuses_empty_live_corpus_over_nonempty_archive(db, tmp_path):
    conn, path = db
    out = Path(tmp_path) / "existing.jsonl"
    out.write_text('{"_v": 1}\n')
    import pytest
    with pytest.raises(RuntimeError):
        export_corpus(out, db_path=path)      # live corpus empty here
    assert out.read_text() == '{"_v": 1}\n'


def test_restore_rejects_unsupported_version(db, tmp_path):
    conn, path = db
    add_question(conn); conn.commit()
    out = Path(tmp_path) / "future.jsonl"
    lines = export_corpus(out, db_path=path).read_text().splitlines()
    rec = json.loads(lines[0]); rec["_v"] = 99
    future = Path(tmp_path) / "future.jsonl"
    future.write_text(json.dumps(rec) + "\n")
    import pytest
    with pytest.raises(ValueError, match="unsupported"):
        restore_corpus(future, db_path=str(Path(tmp_path) / "x.db"))
