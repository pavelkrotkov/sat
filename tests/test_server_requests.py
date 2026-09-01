"""Request-level coverage for the web UI.

Every handler takes its connection from the `get_conn` dependency. The unit
tests call `sessions.submit_answer` and friends directly, so they cannot see
how the connection reaches the handler - which is exactly where the
per-request-connection refactor broke first.
"""

import pytest
from conftest import add_question

from satprep import server as server_mod
from satprep.db import connect, db_context


@pytest.fixture()
def live(db, monkeypatch):
    """A corpus big enough to build a drill from, wired into the app."""
    conn, path = db
    for i in range(8):
        add_question(
            conn,
            passage=f"passage {i}",
            stem=f"stem {i}?",
            choices=[f"q{i}-{letter}" for letter in "abcd"],
            correct="B",
            source="bluebook_test",
            pool="historical",
        )
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
    from satprep.training.sessions import create_session

    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="req")
    sid = sess["plan"]["session_id"]
    qid = sess["questions"][0]["id"]

    with db_context(live) as conn:
        server_mod.answer(
            None, sid, 0, question_id=qid, letter="B", confidence=3, elapsed_ms=4200, conn=conn
        )

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
    from satprep.training.sessions import create_session, submit_answer

    with pytest.raises(RuntimeError, match="boom"), db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="fail")
        submit_answer(conn, sess["plan"]["session_id"], sess["questions"][0]["id"], "B", 2, 100)
        raise RuntimeError("boom")

    after = connect(live)
    assert after.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    after.close()


# ------------------------------------------------------ connection hygiene --


def test_foreign_keys_enforced_on_every_connection(tmp_path):
    """Regression: `PRAGMA foreign_keys=ON` lived in SCHEMA, which now runs
    once per path instead of once per connect. Every connection after the
    first therefore ran with enforcement off, and orphan rows went straight
    in. The pragma is connection-scoped, so it must be re-applied each time."""
    path = tmp_path / "fk.db"
    first = connect(path)
    first.close()

    second = connect(path)
    assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(Exception, match="FOREIGN KEY"):
        second.execute(
            """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                     confidence, time_ms, mode, attempted_at)
               VALUES ('s', 99999, 'A', 1, 2, 0, 'x', 't')"""
        )
    second.close()


def test_replaced_database_is_reinitialised(tmp_path):
    """A cached path whose file is swapped for an older or partial one must
    not skip the remaining DDL. The version stamp catches what the presence
    of a single table does not."""
    import sqlite3

    path = tmp_path / "swap.db"
    connect(path).close()  # caches the path, stamps user_version

    # a partial database: has `questions`, but no view and no error_tags column
    path.unlink()
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    conn = connect(path)
    assert (
        conn.execute("SELECT 1 FROM sqlite_master WHERE name='effective_question_tags'").fetchone()
        is not None
    )
    assert "error_tags" in {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
    conn.close()


def test_concurrent_first_connect_is_serialised(tmp_path):
    """Two requests against a brand-new database used to race into the DDL
    together and one could lose on the write lock."""
    import threading

    path = tmp_path / "race.db"
    errors = []

    def open_once():
        try:
            connect(path).close()
        except Exception as exc:  # pragma: no cover - the failure we prevent
            errors.append(exc)

    threads = [threading.Thread(target=open_once) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_keyboard_interrupt_rolls_back_and_propagates(tmp_path):
    """db_context catches BaseException so a Ctrl-C partway through a drill
    still rolls back - and re-raises it, so termination stays clean."""
    path = tmp_path / "sig.db"

    with pytest.raises(KeyboardInterrupt), db_context(path) as conn:
        add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"])
        raise KeyboardInterrupt

    after = connect(path)
    assert after.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
    after.close()


def test_write_handlers_commit_before_returning(live):
    """Regression: a yield dependency's teardown runs after the response is
    sent, so /begin returned its 303 before db_context committed. A client
    following the redirect could open a new connection and not see the
    session it had just been told about."""
    from satprep import server as srv

    with db_context(live) as conn:
        response = srv.begin(None, mode="error_clinic", count=2, focus_tag="", conn=conn)
        sid = response.headers["location"].split("/")[-2]

        # a separate connection, as the redirected request would use
        other = connect(live)
        assert other.execute("SELECT COUNT(*) FROM sessions WHERE id=?", (sid,)).fetchone()[0] == 1
        other.close()


def test_rejects_a_database_from_a_newer_build(tmp_path):
    """Running this build's DDL over a future schema and restamping the
    marker would silently downgrade the file."""
    from satprep import db as db_mod

    path = tmp_path / "future.db"
    connect(path).close()
    ahead = connect(path)
    ahead.execute(f"PRAGMA user_version={db_mod.SCHEMA_VERSION + 5}")
    ahead.commit()
    ahead.close()
    db_mod._SCHEMA_APPLIED.discard(str(path.resolve()))

    with pytest.raises(RuntimeError, match="newer satprep"):
        connect(path)
