"""The Question value.

Five modules used to decode `choices_json` independently and a sqlite3.Row
travelled from the database into the Jinja templates. The point of hydrating
once is that a missing column fails at the seam, naming the column, instead
of surfacing as an IndexError deep inside a comprehension - which is how the
dashboard crash in #10 stayed hidden.
"""

import dataclasses
import json

import pytest
from conftest import add_question

from satprep.corpus.questions import Choice, Question, iter_active, load, load_many


def test_from_row_decodes_the_json_columns(db):
    conn, _ = db
    qid = add_question(
        conn,
        passage="Passage",
        stem="Stem?",
        choices=["alpha", "beta", "gamma", "delta"],
        correct="C",
        source="bluebook_test",
        pool="historical",
        difficulty="hard",
        skill="Inferences",
    )
    conn.execute(
        "UPDATE questions SET images_json=?, provenance_json=? WHERE id=?",
        ('["artifacts/images/fig.svg"]', '{"bluebook_uid": "u1"}', qid),
    )

    q = load(conn, qid)

    assert isinstance(q, Question)
    assert q.choices == (
        Choice("A", "alpha", False),
        Choice("B", "beta", False),
        Choice("C", "gamma", True),
        Choice("D", "delta", False),
    )
    assert q.images == ("artifacts/images/fig.svg",)
    assert q.provenance == {"bluebook_uid": "u1"}
    assert (q.difficulty, q.official_skill, q.pool) == ("hard", "Inferences", "historical")


def test_missing_column_fails_at_the_seam(db):
    """Regression class: analytics read r["question_id"] from a projection
    that never selected it, and the IndexError surfaced inside a
    comprehension rather than where the row was built."""
    conn, _ = db
    add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"])
    projection = conn.execute("SELECT id, passage FROM questions LIMIT 1").fetchone()

    with pytest.raises(KeyError, match=r"no column 'fingerprint'.*not a projection"):
        Question.from_row(projection)


def test_answer_helpers(db):
    conn, _ = db
    qid = add_question(
        conn, passage="p", stem="s?", choices=["alpha", "beta", "gamma", "delta"], correct="B"
    )
    q = load(conn, qid)

    assert q.key == Choice("B", "beta", True)
    assert q.text_of("C") == "gamma"
    assert q.text_of("Z") == ""
    assert q.text_of("") == ""
    assert q.choice_texts == ["alpha", "beta", "gamma", "delta"]

    assert q.is_correct_answer("B") and q.is_correct_answer("b")
    assert not q.is_correct_answer("A")
    assert not q.is_correct_answer("")


def test_choiceless_questions_are_not_displayable(db):
    """Bluebook omits options on correctly-answered reviews, so 425
    historical items are statistics-only and must never reach a drill."""
    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"])
    conn.execute("UPDATE questions SET choices_json='[]' WHERE id=?", (qid,))

    q = load(conn, qid)
    assert q.choices == ()
    assert not q.is_displayable
    assert q.key is None

    assert list(iter_active(conn, displayable_only=True)) == []
    assert len(list(iter_active(conn))) == 1


def test_load_many_is_one_query_keyed_by_id(db):
    conn, _ = db
    ids = [
        add_question(conn, passage=f"p{i}", stem=f"s{i}?", choices=[f"c{i}{x}" for x in "abcd"])
        for i in range(4)
    ]

    loaded = load_many(conn, ids)
    assert set(loaded) == set(ids)
    assert all(loaded[i].id == i for i in ids)

    assert load_many(conn, []) == {}
    assert load_many(conn, [999999]) == {}


def test_load_returns_none_for_a_missing_id(db):
    conn, _ = db
    assert load(conn, 999999) is None


def test_iter_active_respects_pool_and_active_filters(db):
    """The sampler's leakage guarantee is this filter, in SQL - a protected
    item is never loaded outside benchmark mode, so scoring cannot reach it."""
    conn, _ = db
    add_question(
        conn,
        passage="h",
        stem="h?",
        choices=["ha", "hb", "hc", "hd"],
        source="bluebook_test",
        pool="historical",
    )
    add_question(
        conn, passage="f", stem="f?", choices=["fa", "fb", "fc", "fd"], pool="fresh_training"
    )
    prot = add_question(
        conn, passage="p", stem="p?", choices=["pa", "pb", "pc", "pd"], pool="protected_benchmark"
    )

    pools = {q.pool for q in iter_active(conn, pools=("historical", "fresh_training"))}
    assert pools == {"historical", "fresh_training"}

    conn.execute("UPDATE questions SET active=0 WHERE id=?", (prot,))
    assert all(q.id != prot for q in iter_active(conn))


def test_question_is_frozen(db):
    """A hydrated question is a value: writes go through SQL, not the object."""
    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"])
    q = load(conn, qid)

    with pytest.raises(dataclasses.FrozenInstanceError):
        q.passage = "mutated"


def test_choice_round_trips_through_storage_shape(db):
    """archive and diagnose_attempt both want the stored dict shape back."""
    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"], correct="D")
    q = load(conn, qid)

    stored = json.loads(
        conn.execute("SELECT choices_json FROM questions WHERE id=?", (qid,)).fetchone()[
            "choices_json"
        ]
    )
    assert [c.as_dict() for c in q.choices] == stored


def test_question_is_distinct_from_parsed_question():
    """Settled before the refactor: ParsedQuestion is a source-specific parse
    result carrying student_letter - training data - and no identity.
    Question is the stored corpus entity. Merging them would pull a training
    concept back into the corpus package."""
    from satprep.corpus.parse_snapshot import ParsedQuestion

    parsed = {f.name for f in ParsedQuestion.__dataclass_fields__.values()}
    stored = set(Question.__dataclass_fields__)

    assert "student_letter" in parsed and "student_letter" not in stored
    assert {"id", "fingerprint", "pool"} & parsed == set()
    assert {"id", "fingerprint", "pool"} <= stored
