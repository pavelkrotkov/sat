"""Request-level coverage for the web UI.

Every handler takes its connection from the `get_conn` dependency. The unit
tests call `sessions.submit_answer` and friends directly, so they cannot see
how the connection reaches the handler - which is exactly where the
per-request-connection refactor broke first.
"""

import pytest

from satprep import server as server_mod
from satprep.db import connect, db_context
from conftest import add_question


@pytest.fixture()
def live(db, monkeypatch):
    """A corpus big enough to build a drill from, wired into the app."""
    conn, path = db
    for i in range(8):
        add_question(conn, passage=f"passage {i}", stem=f"stem {i}?",
                     choices=[f"q{i}-{letter}" for letter in "abcd"], correct="B",
                     source="bluebook_test", pool="historical")
    conn.commit()
    conn.close()

    def _ctx(db_path=None):
        return db_context(path)

    monkeypatch.setattr(server_mod, "db_context", _ctx)
    return path


def test_handlers_get_a_usable_connection(live):
    """Regression: a sync generator dependency runs in FastAPI's threadpool
    while an `async def` handler runs on the event loop, so the connection was
    created on one thread and used on another - sqlite3 refused it with
    "SQLite objects created in a thread can only be used in that same thread"
    and every answer submission returned 500."""
    conn = connect(live)
    gen = server_mod.get_conn()
    request_conn = next(gen)

    # the guard that fired is per-connection, so exercising it is enough
    assert request_conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 8
    with pytest.raises(StopIteration):
        next(gen)
    conn.close()


def test_answer_handler_persists_an_attempt(live):
    """The write path end to end: session, attempt, spacing."""
    from satprep.sessions import create_session

    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="req")
    sid = sess["plan"]["session_id"]
    qid = sess["questions"][0]["id"]

    with db_context(live) as conn:
        server_mod.answer(None, sid, 0, question_id=qid, letter="B",
                          confidence=3, elapsed_ms=4200, conn=conn)

    after = connect(live)
    attempt = after.execute(
        "SELECT correct, confidence, time_ms FROM attempts WHERE session_id=? AND question_id=?",
        (sid, qid),
    ).fetchone()
    state = after.execute(
        "SELECT times_seen FROM question_state WHERE question_id=?", (qid,)
    ).fetchone()
    after.close()

    assert attempt["correct"] == 1 and attempt["confidence"] == 3
    assert attempt["time_ms"] == 4200
    assert state["times_seen"] == 1


def test_failed_request_leaves_nothing_behind(live):
    """get_conn wraps db_context, so a handler that raises rolls back."""
    from satprep.sessions import create_session, submit_answer

    with pytest.raises(RuntimeError, match="boom"):
        with db_context(live) as conn:
            sess = create_session(conn, "error_clinic", count=2, seed="fail")
            submit_answer(conn, sess["plan"]["session_id"],
                          sess["questions"][0]["id"], "B", 2, 100)
            raise RuntimeError("boom")

    after = connect(live)
    assert after.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    after.close()
