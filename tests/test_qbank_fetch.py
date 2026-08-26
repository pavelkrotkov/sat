from satprep.corpus.qbank_fetch import _normalize, insert_qbank_row
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
    from satprep.corpus.qbank_fetch import known_external_ids

    conn, path = db
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    assert "ext-1" in known_external_ids(conn)


def test_cross_source_duplicate_enriches_choiceless_row(db):
    """Greptile P1: bank item matching a choice-less Bluebook row backfills choices."""
    from satprep.corpus.fingerprint import fingerprint
    from satprep.corpus.qbank_fetch import insert_qbank_row

    # historical stats-only row: no choices, unknown difficulty
    add_question(db[0], passage="shared passage", stem="shared stem?",
                 choices=[], correct="A", source="bluebook_test", pool="historical")
    # force the bank row to collide: compute fingerprint of its content and pre-insert
    from satprep.db import connect as c2
    conn = db[0]
    conn.execute("DELETE FROM questions")
    conn.execute("""INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                     module, passage, stem, choices_json, correct_letter, rationale, images_json,
                     official_domain, official_skill, skill_source, difficulty, pool,
                     seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
                   VALUES ('preseed', 'bluebook_test', 'SAT Practice Test 4', '7', '',
                     'shared passage', 'shared stem?', '[]', 'A', '', '[]',
                     '', '', 'unknown', '', 'historical', 0, 0, '', '2026-01-01', '{}')""")
    fp = fingerprint("shared passage", "shared stem?", ["opt a", "opt b", "opt c", "opt d"])
    # the incoming bank row must hash identically: same passage/stem/choice texts
    row = {"passage": "shared passage", "stem": "shared stem?",
           "choices": [{"letter": l, "text": t, "is_correct": l == "B"}
                       for l, t in zip("ABCD", ["opt a", "opt b", "opt c", "opt d"])],
           "correct": "B", "difficulty": "hard", "skill": "Inferences",
           "domain": "Information and Ideas", "ext_id": "ext-9"}
    outcome = insert_qbank_row(conn, row, batch="b9")
    assert outcome == "duplicate"
    r = conn.execute("SELECT choices_json, difficulty, official_skill, pool, is_new_bank FROM questions WHERE fingerprint='preseed'").fetchone()
    import json as _json
    assert len(_json.loads(r["choices_json"])) == 4   # enriched, now displayable
    assert r["difficulty"] == "hard" and r["official_skill"] == "Inferences"
    assert r["pool"] == "historical"                  # still not counted fresh
    assert r["is_new_bank"] == 0
