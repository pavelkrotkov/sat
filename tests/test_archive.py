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
    assert rec["tags"] == ["qualifier_strength"]
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
    tags = [r["tag"] for r in c2.execute("SELECT tag FROM question_tags")]
    c2.close()
    assert "qualifier_strength" in tags


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
