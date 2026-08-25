from datetime import datetime

import pytest

from satprep.weakness import compute_weakness, get_weakness
from conftest import add_attempt, add_question


def _db_with_tag(conn, tag, attempts):
    qid = add_question(conn, passage="P", stem="S?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical", tags=(tag,))
    for correct, conf, when in attempts:
        add_attempt(conn, qid, correct, confidence=conf, attempted_at=when,
                    session_id=f"h{tag}{correct}{conf}{when}")
    conn.commit()


def test_one_error_does_not_outrank_many(db):
    conn, path = db
    _db_with_tag(conn, "tag_sparse", [(0, 2, "2026-06-01T00:00:00+00:00")])
    qid = None  # second tag on separate question set
    qid2 = add_question(conn, passage="Q", stem="T?", choices=["a", "b", "c", "d"],
                        source="bluebook_test", pool="historical", tags=("tag_dense",))
    for i in range(20):
        add_attempt(conn, qid2, 0 if i < 8 else 1, confidence=2,
                    session_id=f"dense{i}", attempted_at="2026-06-01T00:00:00+00:00")
    conn.commit()
    scores = compute_weakness(conn, now=datetime(2026, 8, 1).astimezone())
    sparse = scores["tag"]["tag_sparse"]["score"]
    dense = scores["tag"]["tag_dense"]["score"]
    # 8/20 wrong (40%) must score HIGHER than 1/1 wrong smoothed hard toward prior
    assert dense > sparse


def test_confident_wrong_outweighs_humble_wrong(db):
    conn, path = db
    qid = add_question(conn, passage="A", stem="A?", choices=["a", "b"], correct="A",
                       source="bluebook_test", pool="historical", tags=("cw",))
    qid2 = add_question(conn, passage="B", stem="B?", choices=["a", "b"], correct="A",
                        source="bluebook_test", pool="historical", tags=("hw",))
    add_attempt(conn, qid, 0, confidence=3)
    add_attempt(conn, qid2, 0, confidence=1)
    conn.commit()
    s = compute_weakness(conn, now=datetime(2026, 9, 1).astimezone())["tag"]
    assert s["cw"]["score"] > s["hw"]["score"]


def test_correct_low_confidence_proves_less_than_confident_correct(db):
    conn, path = db
    q_shaky = add_question(conn, passage="C", stem="C?", choices=["a", "b"], correct="A",
                           source="bluebook_test", pool="historical", tags=("shaky_tag",))
    q_solid = add_question(conn, passage="F", stem="F?", choices=["a", "b"], correct="A",
                           source="bluebook_test", pool="historical", tags=("solid_tag",))
    for i in range(20):
        add_attempt(conn, q_shaky, 1, confidence=1, session_id=f"s{i}")
        add_attempt(conn, q_solid, 1, confidence=3, session_id=f"d{i}")
    conn.commit()
    s = compute_weakness(conn, now=datetime(2026, 9, 1).astimezone())["tag"]
    # guessing-your-way-to-correct leaves more residual weakness than proving it
    assert s["shaky_tag"]["score"] > s["solid_tag"]["score"]
    assert s["solid_tag"]["score"] < 20


def test_recency_decay_reduces_old_errors(db):
    conn, path = db
    q_old = add_question(conn, passage="D", stem="D?", choices=["a", "b"], correct="A",
                         source="bluebook_test", pool="historical", tags=("old_err",))
    q_new = add_question(conn, passage="E", stem="E?", choices=["a", "b"], correct="A",
                         source="bluebook_test", pool="historical", tags=("new_err",))
    add_attempt(conn, q_old, 0, 2, session_id="o", attempted_at="2025-09-01T00:00:00+00:00")
    add_attempt(conn, q_new, 0, 2, session_id="n", attempted_at="2026-07-30T00:00:00+00:00")
    conn.commit()
    s = compute_weakness(conn, now=datetime(2026, 8, 1).astimezone())["tag"]
    assert s["new_err"]["score"] > s["old_err"]["score"]
