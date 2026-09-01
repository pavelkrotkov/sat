"""Issue #49 regression tests: Bluebook history repair.

These cover the three defects the repair fixes:
1. both modal layouts yield choices from the saved HTML;
2. JSON fallback/field merge fills gaps independently;
3. identical-content occurrences keep their placement identity.
"""

import json

import pytest

from satprep.clock import utc_now
from satprep.corpus.parse_snapshot import ParsedQuestion, parse_snapshot
from satprep.corpus.repair import merge_fields, repair_bluebook
from satprep.db import db_context

# --------------------------------------------------------------- fixtures --

CORRECT_HTML = """<div class="cb-modal-content"><div class="question-content "><div class="question-panel"><h3 class="cb-margin-bottom-16">Reading and Writing: Question 1</h3><div><p>Passage text for the correct review.</p></div><div><p>Which choice completes the text with the most logical and precise word or phrase?</p></div><ol type="A" class="answer-options cb-margin-top-16"><li class="correct"><div><p>widespread</p></div></li><li class=""><div><p>careful</p></div></li><li class=""><div><p>unintended</p></div></li><li class=""><div><p>infrequent</p></div></li></ol></div><div class="answer-panel"><div class="provided-answer"><h3 class="cb-margin-bottom-16">Answer</h3><p class="correct response cb-margin-bottom-16">You selected answer A. The correct answer is A.</p></div><div class="rationale"><h3 class="cb-margin-bottom-16">Rationale</h3><div><p>Choice A is the best answer.</p></div></div></div></div></div>"""

INCORRECT_HTML = """<div class="cb-modal-content"><div class="question-content"><div class="question-panel"><h3 class="cb-margin-bottom-16">Reading and Writing: Question 7</h3><div><p>Passage text for the incorrect review.</p></div><div><p>What does the text most strongly suggest?</p></div></div><div class="answer-panel"><h3 class="cb-margin-bottom-16">Answer</h3><ol type="A" class="cb-margin-bottom-16"><li class=""><div><p>alpha</p></div></li><li class="correct"><div><p>beta</p></div></li><li class=""><div><p>gamma</p></div></li><li class=""><div><p>delta</p></div></li></ol><div class=""><p class="incorrect response cb-margin-bottom-16">You selected answer B. The correct answer is B.</p></div><div class="rationale"><h3 class="cb-margin-bottom-16">Rationale</h3><div><p>Choice B is correct because beta.</p></div></div></div></div></div>"""


def _rec(
    uid,
    *,
    status="Correct",
    my="A; Correct",
    key="A",
    question_text="Passage from JSON.",
    choices=(),
    snap=None,
    module="Module 1",
    num="1",
    test="SAT Practice Test 4",
    explanation="JSON rationale.",
):
    return {
        "uid": uid,
        "scraped_at": "2026-03-01T00:00:00+00:00",
        "test_name": test,
        "test_number": "4",
        "section": "Reading and Writing",
        "subject_bucket": "Reading and Writing",
        "module": module,
        "question_number": str(num),
        "domain": "",
        "skill": "",
        "my_answer": my,
        "correct_answer": key,
        "answer_status": status,
        "question_text": question_text,
        "answer_choices": list(choices),
        "explanation": explanation,
        "images": [],
        "html_snapshot_path": snap or "",
    }


@pytest.fixture()
def snap_dir(tmp_path, monkeypatch):
    d = tmp_path / "artifacts" / "html"
    d.mkdir(parents=True)
    monkeypatch.setattr("satprep.config.SNAPSHOT_DIR", d)
    return d


# ------------------------------------------------------------ parser -----


def test_parse_correct_layout_yields_choices():
    p = parse_snapshot(CORRECT_HTML)
    assert len(p.choices) == 4
    assert [c["letter"] for c in p.choices] == list("ABCD")
    assert p.correct_letter == "A"
    assert p.student_letter == "A"
    assert p.rationale


def test_parse_incorrect_layout_yields_choices():
    p = parse_snapshot(INCORRECT_HTML)
    assert len(p.choices) == 4
    assert p.correct_letter == "B"
    assert p.student_letter == "B"
    assert p.rationale


def test_parse_prefers_answer_panel_over_passage_list():
    """T7: a generic ordered list inside the passage must not be treated as
    the answer choices; only .question-panel ol.answer-options is a choice
    list, otherwise fall back to .answer-panel ol."""
    html = """<div class="question-panel"><h3>Reading and Writing: Question 2</h3>
      <div><p>Rank these steps:</p><ol><li>first</li><li>second</li><li>third</li></ol></div>
      <div><p>The stem text follows?</p></div></div>
      <div class="answer-panel"><h3>Answer</h3>
      <ol type="A" class="cb-margin-bottom-16">
        <li class=""><div><p>alpha</p></div></li>
        <li class="correct"><div><p>beta</p></div></li>
        <li class=""><div><p>gamma</p></div></li>
      </ol>
      <p class="incorrect response">You selected answer B. The correct answer is B.</p>
      </div>"""
    p = parse_snapshot(html)
    # passage list (first/second/third) is NOT parsed as choices
    assert [c["text"] for c in p.choices] == ["alpha", "beta", "gamma"]
    assert p.correct_letter == "B"


# ------------------------------------------------------------- merge -----


def test_merge_snapshot_wins_and_json_fills_gaps():
    parsed = parse_snapshot(CORRECT_HTML)
    rec = _rec("u1", question_text="JSON passage.", choices=[])
    merged, warnings = merge_fields(parsed, rec)
    # snapshot passage beats JSON
    assert merged["passage"] == "Passage text for the correct review."
    # snapshot choices win over empty JSON choices
    assert len(merged["choices"]) == 4
    assert merged["correct_letter"] == "A"
    assert merged["rationale"] == "Choice A is the best answer."
    assert warnings == []


def test_merge_json_fills_missing_snapshot_choices():
    parsed = parse_snapshot(CORRECT_HTML)
    parsed.choices = []
    parsed.correct_letter = ""
    rec = _rec("u1", choices=["A. from json", "B. also json"])
    merged, warnings = merge_fields(parsed, rec)
    assert len(merged["choices"]) == 2
    assert merged["correct_letter"] == "A"
    assert "no answer choices" not in " ".join(warnings)


def test_merge_warns_when_field_unavailable_everywhere():
    parsed = ParsedQuestion(passage="P", stem="")
    rec = _rec("u1", question_text="", choices=[], key="", explanation="")
    _merged, warnings = merge_fields(parsed, rec)
    joined = " ".join(warnings)
    assert "no answer choices" in joined
    assert "no answer key" in joined
    assert "no rationale" in joined


# ------------------------------------------------------------ repair -----


def _write_sources(tmp_path, records, snap_map=None):
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    (out / "wrong_questions.json").write_text(json.dumps(records))
    if snap_map:
        d = tmp_path / "artifacts" / "html"
        d.mkdir(parents=True, exist_ok=True)
        for name, html in snap_map.items():
            (d / name).write_text(html)


def _abs_snap(tmp_path, name):
    """Records normally store repo-root-relative snapshot paths; the
    tests use absolute paths under tmp_path so repair finds them."""
    return str(tmp_path / "artifacts" / "html" / name)


def _repair(tmp_path, records, snap_map=None, monkeypatch=None):
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, records, snap_map)
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        return repair_bluebook(conn)


def test_repair_reconciles_choice_less_rows_in_place(tmp_path, monkeypatch):
    """A previously-ingested choice-less row (correct review) must be
    reconciled to the SAME question_id with choices recovered from HTML,
    keeping attempts attached."""
    snap = "sat-practice-test-4-reading-and-writing-module-1-1-1-reading-and-writing-correct.html"
    rec = _rec("sat-...-correct", snap=_abs_snap(tmp_path, snap))

    # First ingest with the OLD broken parser behaviour: no choices recovered.
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, [rec], {snap: CORRECT_HTML})
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        # Simulate the old bug: parser returned zero choices, so the row
        # was inserted choice-less with a passage-only fingerprint.
        from satprep.clock import utc_now
        from satprep.corpus.fingerprint import fingerprint

        fp = fingerprint(rec["question_text"], "", [])
        cur = conn.execute(
            """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                 module, passage, stem, choices_json, correct_letter, rationale, images_json,
                 official_domain, official_skill, skill_source, difficulty, pool, seen_benchmark,
                 is_new_bank, import_batch, imported_at, provenance_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,'',?,?)""",
            (
                fp,
                "bluebook_test",
                rec["test_name"],
                rec["question_number"],
                rec["module"],
                rec["question_text"],
                "",
                "[]",
                "A",
                "",
                "[]",
                "",
                "",
                "unknown",
                "",
                "historical",
                utc_now(),
                json.dumps({"bluebook_uid": rec["uid"]}),
            ),
        )
        qid = cur.lastrowid
        conn.execute(
            """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                     confidence, time_ms, mode, attempted_at)
               VALUES (?,?,?,?,0,0,'historical',?)""",
            (f"hist:{rec['uid']}", qid, "A", 1, "2026-03-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,?,?)",
            (qid, "manual_tag", "manual", "2026-01-01"),
        )

    # Repair: same record + snapshot that now parses choices.
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    with db_context(dbp) as conn:
        stats = repair_bluebook(conn)
    assert stats["rows_updated"] == 1

    with db_context(dbp) as conn:
        row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        assert json.loads(row["choices_json"])  # choices recovered
        assert len(json.loads(row["choices_json"])) == 4
        assert row["correct_letter"] == "A"
        # attempts and tags still attached to the SAME row
        assert (
            conn.execute("SELECT COUNT(*) FROM attempts WHERE question_id=?", (qid,)).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM question_tags WHERE question_id=?", (qid,)
            ).fetchone()[0]
            == 1
        )
        # occurrence recorded with the uid
        occ = conn.execute(
            "SELECT bluebook_uid, question_id FROM bluebook_occurrences WHERE bluebook_uid=?",
            (rec["uid"],),
        ).fetchone()
        assert occ is not None and occ["question_id"] == qid


def test_repair_is_idempotent(tmp_path, monkeypatch):
    snap = "sat-practice-test-4-reading-and-writing-module-1-1-1-reading-and-writing-correct.html"
    rec = _rec("sat-...-correct", snap=_abs_snap(tmp_path, snap))
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, [rec], {snap: CORRECT_HTML})
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        s1 = repair_bluebook(conn)
    with db_context(dbp) as conn:
        s2 = repair_bluebook(conn)
    assert s1["rows_inserted"] == 1
    assert s2["rows_inserted"] == 0 and s2["rows_updated"] == 0
    assert s2["occurrences_upserted"] == 1
    with db_context(dbp) as conn:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        occ = conn.execute("SELECT COUNT(*) FROM bluebook_occurrences").fetchone()[0]
    assert n == 1 and occ == 1


def test_repair_records_both_layouts(tmp_path, monkeypatch):
    snap_c = "test-4-m1-q1-correct.html"
    snap_i = "test-4-m2-q7-incorrect.html"
    recs = [
        _rec(
            "uid-c", snap=_abs_snap(tmp_path, snap_c), status="Correct", num="1", module="Module 1"
        ),
        _rec(
            "uid-i",
            snap=_abs_snap(tmp_path, snap_i),
            status="Incorrect",
            my="B; Incorrect",
            key="B",
            num="7",
            module="Module 2",
        ),
    ]
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, recs, {snap_c: CORRECT_HTML, snap_i: INCORRECT_HTML})
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        stats = repair_bluebook(conn)
    assert stats["rows_inserted"] == 2
    with db_context(dbp) as conn:
        rows = conn.execute(
            "SELECT q.stem, q.correct_letter, json_array_length(q.choices_json) AS n "
            "FROM questions q ORDER BY q.id"
        ).fetchall()
        assert {r["n"] for r in rows} == {4}
        assert {r["correct_letter"] for r in rows} == {"A", "B"}


def test_repair_reuses_fingerprint_for_identical_content(tmp_path, monkeypatch):
    """T0: two UIDs with identical content share one question row instead of
    tripping the fingerprint UNIQUE constraint."""
    snap = "test-dup.html"
    recs = [
        _rec("dup-a", snap=_abs_snap(tmp_path, snap), status="Correct", num="1"),
        _rec(
            "dup-b",
            snap=_abs_snap(tmp_path, snap),
            status="Incorrect",
            my="B; Incorrect",
            key="A",
            num="1",
        ),
    ]
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, recs, {snap: CORRECT_HTML})
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        stats = repair_bluebook(conn)
    assert stats["rows_inserted"] == 1
    # identical content -> nothing to reconcile on the second record
    assert stats["rows_updated"] == 0
    with db_context(dbp) as conn:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        occ = conn.execute("SELECT COUNT(*) FROM bluebook_occurrences").fetchone()[0]
    assert n == 1 and occ == 2


def test_reconcile_keeps_stored_choices_when_merge_has_none(tmp_path, monkeypatch):
    """T2: an existing row that already has choices must not have them
    erased when the snapshot is missing and JSON has none."""
    snap = "test-present.html"
    rec = _rec(
        "uid", snap=_abs_snap(tmp_path, snap), status="Correct", question_text="", choices=[]
    )
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, [rec], {snap: CORRECT_HTML})
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        repair_bluebook(conn)
        qid = conn.execute("SELECT id FROM questions").fetchone()[0]
    # remove the snapshot so a re-run cannot parse it -> merged choices empty
    import os

    os.remove(tmp_path / "artifacts" / "html" / snap)
    with db_context(dbp) as conn:
        new_rec = dict(rec)
        new_rec["question_text"] = ""
        s2 = repair_bluebook(conn)
        row = conn.execute("SELECT choices_json FROM questions WHERE id=?", (qid,)).fetchone()
    assert json.loads(row["choices_json"])  # stored choices preserved
    assert s2["rows_updated"] == 0


def test_json_fallback_marks_key(tmp_path, monkeypatch):
    """T8: JSON fallback choices must mark the correct answer as is_correct."""
    rec = _rec(
        "u",
        status="Incorrect",
        my="B; Incorrect",
        key="A",
        question_text="",
        choices=["A. correct one", "B. distractor"],
    )
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, [rec])
    dbp = tmp_path / "t.db"
    with db_context(dbp) as conn:
        repair_bluebook(conn)
        row = conn.execute("SELECT choices_json, correct_letter FROM questions").fetchone()
    choices = json.loads(row["choices_json"])
    assert [c["is_correct"] for c in choices] == [True, False]
    assert row["correct_letter"] == "A"


def test_repair_backfills_error_diagnosis(tmp_path, monkeypatch):
    """T9: a pre-existing wrong historical attempt gets error diagnosis once
    choices are recovered by a later repair."""
    wrong_html = """<div class="question-panel"><h3>Reading and Writing: Question 1</h3>
      <div><p>Passage for the wrong answer.</p></div>
      <div><p>Which choice best describes the study?</p></div>
      <ol type="A" class="answer-options"><li class="correct"><p>The study shows students may benefit from the program.</p></li>
      <li class=""><p>The study proves students always benefit from the program.</p></li></ol></div>
      <div class="answer-panel">
      <p class="incorrect response">You selected answer B. The correct answer is A.</p>
      <div class="rationale"><h3>Rationale</h3><p>A is correct.</p></div></div>"""
    snap = "test-diag.html"
    rec = _rec(
        "diag",
        snap=_abs_snap(tmp_path, snap),
        status="Incorrect",
        my="B; Incorrect",
        key="A",
        question_text="",
    )
    monkeypatch.setattr(
        "satprep.config.BLUEBOOK_JSON", tmp_path / "outputs" / "wrong_questions.json"
    )
    _write_sources(tmp_path, [rec], {snap: wrong_html})
    dbp = tmp_path / "t.db"
    # Seed a legacy choice-less row + wrong attempt, mirroring pre-repair state.
    from satprep.corpus.fingerprint import fingerprint as fpmod_fp

    with db_context(dbp) as conn:
        legacy_fp = fpmod_fp("", "", [])
        cur = conn.execute(
            """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                 module, passage, stem, choices_json, correct_letter, rationale, images_json,
                 official_domain, official_skill, skill_source, difficulty, pool, seen_benchmark,
                 is_new_bank, import_batch, imported_at, provenance_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,'',?,?)""",
            (
                legacy_fp,
                "bluebook_test",
                rec["test_name"],
                rec["question_number"],
                rec["module"],
                "",
                "",
                "[]",
                "A",
                "",
                "[]",
                "",
                "",
                "unknown",
                "",
                "historical",
                utc_now(),
                json.dumps({"bluebook_uid": rec["uid"]}),
            ),
        )
        qid = cur.lastrowid
        conn.execute(
            """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                     confidence, time_ms, mode, attempted_at)
               VALUES (?,?,?,?,0,0,'historical',?)""",
            (f"hist:{rec['uid']}", qid, "B", 0, "2026-03-01T00:00:00+00:00"),
        )
    with db_context(dbp) as conn:
        stats = repair_bluebook(conn)
        n_diag = conn.execute("SELECT COUNT(*) FROM student_error_tags").fetchone()[0]
    assert stats["rows_updated"] >= 1  # choices + fingerprint reconciled
    assert n_diag > 0  # diagnosis backfilled for the repaired wrong attempt
