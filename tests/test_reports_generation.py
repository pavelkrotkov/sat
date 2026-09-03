import threading
import time
from pathlib import Path

import pytest
from conftest import add_question
from fastapi import HTTPException

from satprep import reports as reports_mod
from satprep import server as server_mod
from satprep.db import connect


def add_report_attempt(conn, qid, *, correct=False, mode="targeted_drill", marker=""):
    cursor = conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                 confidence, time_ms, mode, attempted_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            f"report:{marker}:{qid}",
            qid,
            "B" if correct else "A",
            int(correct),
            2,
            42000,
            mode,
            "2026-03-01T00:00:00+00:00",
        ),
    )
    return cursor.lastrowid


def add_report_question(conn, marker, *, skill="Inferences"):
    qid = add_question(
        conn,
        passage=f"passage {marker}",
        stem=f"question {marker}?",
        choices=["wrong", "right", "other", "last"],
        correct="B",
        source="custom_generated",
        pool="fresh_training",
        skill=skill,
    )
    conn.execute("UPDATE questions SET official_domain='Information and Ideas' WHERE id=?", (qid,))
    return qid


def use_report_dir(monkeypatch, tmp_path: Path):
    directory = tmp_path / "reports"
    monkeypatch.setattr(reports_mod, "REPORTS_DIR", directory)
    return directory


def test_first_run_filters_history_and_commits_fixed_interval(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    historical_qid = add_report_question(conn, "historical")
    eligible_qid = add_report_question(conn, "eligible")
    historical_id = add_report_attempt(conn, historical_qid, mode="historical", marker="history")
    eligible_id = add_report_attempt(conn, eligible_qid, marker="eligible")
    conn.commit()

    result = reports_mod.run_report_generation(conn)

    assert result.status == reports_mod.COMPLETED
    assert result.after_attempt_id == 0
    assert result.through_attempt_id == max(historical_id, eligible_id)
    assert result.eligible_attempt_count == 1
    assert result.wrong_count == 1
    assert reports_mod.get_report_watermark(conn) == result.through_attempt_id
    body = result.report_path.read_text()
    assert "passage eligible" in body
    assert "passage historical" not in body
    run = conn.execute(
        "SELECT status, report_name FROM report_runs WHERE id=?", (result.run_id,)
    ).fetchone()
    assert run["status"] == "completed"
    assert run["report_name"] == result.report_name


def test_attempt_arriving_during_generation_is_deferred_to_next_run(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    first_qid = add_report_question(conn, "first")
    first_id = add_report_attempt(conn, first_qid, marker="first")
    conn.commit()
    original = reports_mod.generate_error_report
    captured = {}

    def add_late_attempt(connection, **kwargs):
        captured.update(kwargs)
        late_qid = add_report_question(connection, "late")
        late_id = add_report_attempt(connection, late_qid, marker="late")
        connection.commit()
        captured["late_id"] = late_id
        return original(connection, **kwargs)

    monkeypatch.setattr(reports_mod, "generate_error_report", add_late_attempt)
    first = reports_mod.run_report_generation(conn)
    monkeypatch.setattr(reports_mod, "generate_error_report", original)

    assert first.through_attempt_id == first_id
    assert captured["late_id"] > first.through_attempt_id
    assert "passage late" not in first.report_path.read_text()

    second = reports_mod.run_report_generation(conn)
    assert second.after_attempt_id == first.through_attempt_id
    assert second.through_attempt_id == captured["late_id"]
    assert second.eligible_attempt_count == 1
    assert "passage late" in second.report_path.read_text()


def test_empty_historical_only_and_zero_miss_runs_have_deliberate_outcomes(
    db, monkeypatch, tmp_path
):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)

    empty = reports_mod.run_report_generation(conn)
    assert empty.status == reports_mod.NO_OP
    assert empty.message == "Nothing new to review."
    assert reports_mod.get_report_watermark(conn) == 0

    historical_qid = add_report_question(conn, "only-history")
    historical_id = add_report_attempt(
        conn, historical_qid, mode="historical", marker="only-history"
    )
    conn.commit()
    historical = reports_mod.run_report_generation(conn)
    assert historical.status == reports_mod.NO_OP
    assert "historical" in historical.message
    assert historical.through_attempt_id == historical_id
    assert reports_mod.get_report_watermark(conn) == historical_id
    assert historical.report_path is None

    correct_qid = add_report_question(conn, "correct")
    correct_id = add_report_attempt(conn, correct_qid, correct=True, marker="correct")
    conn.commit()
    success = reports_mod.run_report_generation(conn)
    assert success.status == reports_mod.COMPLETED
    assert success.after_attempt_id == historical_id
    assert success.through_attempt_id == correct_id
    assert success.eligible_attempt_count == 1
    assert success.wrong_count == 0
    assert "No mistakes in this interval" in success.report_path.read_text()


def test_failed_generation_does_not_advance_watermark_and_retries_safely(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    qid = add_report_question(conn, "retry")
    attempt_id = add_report_attempt(conn, qid, marker="retry")
    conn.commit()
    original = reports_mod.generate_error_report

    def fail(*_args, **_kwargs):
        raise RuntimeError("diagnostic exploded")

    monkeypatch.setattr(reports_mod, "generate_error_report", fail)
    failed = reports_mod.run_report_generation(conn)
    assert failed.status == reports_mod.FAILED
    assert "diagnostic exploded" in failed.error
    assert reports_mod.get_report_watermark(conn) == 0
    row = conn.execute(
        "SELECT status, error FROM report_runs WHERE id=?", (failed.run_id,)
    ).fetchone()
    assert row["status"] == "failed"
    assert "diagnostic exploded" in row["error"]

    monkeypatch.setattr(reports_mod, "generate_error_report", original)
    retried = reports_mod.run_report_generation(conn)
    assert retried.status == reports_mod.COMPLETED
    assert retried.after_attempt_id == 0
    assert retried.through_attempt_id == attempt_id
    assert reports_mod.get_report_watermark(conn) == attempt_id


def test_stale_running_run_is_recovered_without_skipping_work(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    conn.execute(
        """INSERT INTO report_runs
           (status, started_at, after_attempt_id, through_attempt_id,
            eligible_attempt_count, wrong_count, lease_expires_at, lease_token)
           VALUES ('running', '2020-01-01T00:00:00+00:00', 0, 0, 0, 0,
                   '2020-01-01T00:01:00+00:00', 'stale-token')"""
    )
    conn.commit()

    result = reports_mod.run_report_generation(conn)

    assert result.status == reports_mod.NO_OP
    old = conn.execute("SELECT status, error FROM report_runs WHERE lease_token IS NULL").fetchall()
    assert any(row["status"] == "failed" and "stale" in row["error"] for row in old)


def test_concurrent_generation_has_one_database_lease(db, monkeypatch, tmp_path):
    conn, path = db
    use_report_dir(monkeypatch, tmp_path)
    qid = add_report_question(conn, "concurrent")
    add_report_attempt(conn, qid, marker="concurrent")
    conn.commit()
    original = reports_mod.generate_error_report
    entered = threading.Event()
    release = threading.Event()
    first_result = []

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(reports_mod, "generate_error_report", slow)

    def first_worker():
        worker_conn = connect(path)
        try:
            first_result.append(reports_mod.run_report_generation(worker_conn))
        finally:
            worker_conn.close()

    thread = threading.Thread(target=first_worker)
    thread.start()
    assert entered.wait(5)
    second_conn = connect(path)
    second = reports_mod.run_report_generation(second_conn)
    second_conn.close()
    release.set()
    thread.join(5)
    monkeypatch.setattr(reports_mod, "generate_error_report", original)

    assert not thread.is_alive()
    assert first_result and first_result[0].status == reports_mod.COMPLETED
    assert second.status == reports_mod.IN_PROGRESS
    assert (
        conn.execute("SELECT COUNT(*) FROM report_runs WHERE status='completed'").fetchone()[0] == 1
    )


def test_live_generator_outliving_lease_is_not_reacquired(db, monkeypatch, tmp_path):
    """A generator that outlives the nominal TTL stays owner via heartbeat renewal."""
    conn, path = db
    use_report_dir(monkeypatch, tmp_path)
    qid = add_report_question(conn, "heartbeat")
    add_report_attempt(conn, qid, marker="heartbeat")
    conn.commit()
    original = reports_mod.generate_error_report
    entered = threading.Event()
    release = threading.Event()
    first_result = []

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(reports_mod, "generate_error_report", slow)

    lease_seconds = 2  # nominal TTL far shorter than the generator is alive
    # Heartbeat interval is derived as max(1.0, lease_seconds/3).
    actual_interval = max(1.0, lease_seconds / 3.0)
    assert actual_interval == 1.0

    def first_worker():
        worker_conn = connect(path)
        try:
            first_result.append(
                reports_mod.run_report_generation(worker_conn, lease_seconds=lease_seconds)
            )
        finally:
            worker_conn.close()

    thread = threading.Thread(target=first_worker)
    thread.start()
    assert entered.wait(5)
    # Let the nominal 2s lease lapse fully while the first worker is still
    # inside generation. Without heartbeat renewal, the next acquisition would
    # reap it as stale and start an overlapping duplicate run on (0, 1].
    time.sleep(3.0)
    second_conn = connect(path)
    second = reports_mod.run_report_generation(second_conn, lease_seconds=lease_seconds)
    second_conn.close()
    release.set()
    thread.join(5)
    monkeypatch.setattr(reports_mod, "generate_error_report", original)

    assert not thread.is_alive()
    # The live worker completed its interval and is the sole owner of it.
    assert first_result and first_result[0].status == reports_mod.COMPLETED
    assert first_result[0].through_attempt_id == 1
    assert second.status == reports_mod.IN_PROGRESS
    rows = conn.execute(
        "SELECT status, after_attempt_id, through_attempt_id FROM report_runs ORDER BY id"
    ).fetchall()
    completed = [r for r in rows if r["status"] == "completed"]
    assert len(completed) == 1
    assert (completed[0]["after_attempt_id"], completed[0]["through_attempt_id"]) == (0, 1)
    # The concurrent request never acquired a lease of its own.
    assert len(rows) == 1


def test_latest_and_serving_require_completed_database_registration(db, monkeypatch, tmp_path):
    conn, _path = db
    directory = use_report_dir(monkeypatch, tmp_path)
    qid = add_report_question(conn, "registered")
    add_report_attempt(conn, qid, marker="registered")
    conn.commit()
    result = reports_mod.run_report_generation(conn)
    orphan = directory / "weekly-orphan.html"
    orphan.write_text("orphan")

    assert reports_mod.latest_report(conn) == result.report_path
    response = server_mod.reports_serve(None, result.report_name, conn=conn)
    assert response.media_type == "text/html"
    with pytest.raises(HTTPException) as exc:
        server_mod.reports_serve(None, orphan.name, conn=conn)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        server_mod.reports_serve(None, "../secret.html", conn=conn)


def test_report_page_exposes_generation_trigger(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    response = server_mod.reports_index(None, conn=conn)
    body = response.body.decode()
    assert 'method="post" action="/reports/generate"' in body
    assert "Review mistakes since the last report" in body


def test_generation_route_redirects_to_fresh_report(db, monkeypatch, tmp_path):
    conn, _path = db
    use_report_dir(monkeypatch, tmp_path)
    qid = add_report_question(conn, "route")
    add_report_attempt(conn, qid, marker="route")
    conn.commit()

    response = server_mod.reports_generate(None, conn=conn)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/reports/weekly-")
    assert conn.execute("SELECT status FROM report_runs").fetchone()[0] == "completed"
