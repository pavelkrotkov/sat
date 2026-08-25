from satprep.qbank_fetch import _normalize, insert_qbank_row
from conftest import add_question


META = {"external_id": "ext-1", "primary_class_cd_desc": "Information and Ideas",
        "skill_desc": "Inferences", "difficulty": "H"}
DETAIL = {
    "type": "mcq",
    "stem": "<p>Which choice most logically completes the text?</p>",
    "stimulus": "<p>A stimulus &mdash; with entities.</p>",
    "answerOptions": [{"id": "k1", "content": "<p>opt A</p>"},
                      {"id": "k2", "content": "<p>opt B</p>"},
                      {"id": "k3", "content": "<p>opt C</p>"},
                      {"id": "k4", "content": "<p>opt D</p>"}],
    "keys": ["k2"],
    "rationale": "<p>Choice B is the best answer.</p>",
    "externalid": "ext-1",
}


def test_normalize_extracts_everything():
    row = _normalize(DETAIL, META)
    assert row["correct"] == "B"
    assert [c["letter"] for c in row["choices"]] == list("ABCD")
    assert row["choices"][1]["is_correct"]
    assert row["difficulty"] == "hard"
    assert row["skill"] == "Inferences"
    assert "&mdash;" not in row["passage"] and "—" in row["passage"]


def test_normalize_rejects_missing_key():
    bad = {k: v for k, v in DETAIL.items() if k != "keys"}
    assert _normalize(bad, META) is None


def test_insert_dedupes_and_pools(db):
    conn, path = db
    row = _normalize(DETAIL, META)
    first = insert_qbank_row(conn, row, batch="b1")
    second = insert_qbank_row(conn, row, batch="b2")
    assert first == "added" and second == "duplicate"
    r = conn.execute("SELECT pool, is_new_bank, import_batch FROM questions").fetchone()
    assert r["pool"] in ("fresh_training", "protected_benchmark")
    assert r["is_new_bank"] == 1 and r["import_batch"] == "b1"


def test_insert_rejects_incomplete(db):
    conn, path = db
    assert insert_qbank_row(conn, {"choices": [], "correct": ""}, batch="b") == "invalid"


def test_known_external_ids_roundtrip(db):
    from satprep.qbank_fetch import known_external_ids

    conn, path = db
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    assert "ext-1" in known_external_ids(conn)
