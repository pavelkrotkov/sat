import json
import threading
from types import SimpleNamespace

from conftest import add_question

from satprep import report_coaching_analysis as coaching_analysis
from satprep import reports as reports_mod
from satprep import server as server_mod


def _attempt(conn, qid, *, correct, time_ms, tags=(), reason="", confidence=2):
    return conn.execute(
        """INSERT INTO attempts
           (session_id, question_id, chosen_letter, correct, confidence, time_ms,
            mode, attempted_at, error_tags, self_report_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            f"issue57:{qid}:{time_ms}",
            qid,
            "B" if correct else "A",
            int(correct),
            confidence,
            time_ms,
            "targeted_drill",
            "2026-09-06T12:00:00+00:00",
            json.dumps(list(tags)),
            reason,
        ),
    ).lastrowid


def test_report_aggregates_canonical_errors_and_timing(db, monkeypatch, tmp_path):
    conn, _ = db
    q1 = add_question(
        conn,
        passage="One effect was found. However, a second result limited it.",
        choices=("This always proves it.", "The narrower claim is supported.", "x", "y"),
        correct="B",
        source="custom_generated",
        pool="fresh_training",
        skill="Inferences",
    )
    q2 = add_question(
        conn,
        passage="Group A increased while group B decreased.",
        choices=("B increased.", "A increased.", "x", "y"),
        correct="B",
        source="custom_generated",
        pool="fresh_training",
        skill="Command of Evidence",
    )
    _attempt(
        conn,
        q1,
        correct=False,
        time_ms=50_000,
        tags=("over_inference",),
        reason="narrowed to two",
        confidence=3,
    )
    _attempt(conn, q1, correct=True, time_ms=80_000)
    through = _attempt(conn, q2, correct=False, time_ms=120_000, tags=("direction_reversal",))
    conn.commit()
    monkeypatch.setattr(reports_mod, "REPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        coaching_analysis,
        "explain_error",
        lambda **_: SimpleNamespace(
            mode="rule",
            tempting_answer="legacy defect text",
            exact_failure="reverses the tested relationship",
            correct_reasoning="Keep the relationship in the direction stated by the evidence.",
            error_taxonomy=[],
        ),
    )

    result = reports_mod.generate_error_report(
        conn, after_attempt_id=0, through_attempt_id=through, reports_dir=tmp_path
    )
    body = result.report_path.read_text()

    assert "Unsupported addition / over-inference — 1 miss (50%)" in body
    assert "Wrong relationship / direction — 1 miss (50%)" in body
    assert "Current coaching rules" in body and "What is actually costing points" in body
    assert "&lt;60 sec" in body
    assert "1:00–1:45" in body and "1:45–2:30" in body and "Error rate" in body
    assert "Dumb summary" in body and "Prediction before choices" in body
    assert "Distractor bait" in body and "Fatal defect" in body and "Next-time rule" in body
    assert "narrowed to two" in body and "high-value confident miss" in body
    assert "<details><summary>Show official College Board rationale</summary>" in body


def test_wrong_answer_reason_is_saved_after_feedback(db):
    conn, _ = db
    qid = add_question(conn, source="custom_generated", pool="fresh_training", correct="B")
    conn.execute(
        """INSERT INTO sessions (id, mode, created_at, seed, algo_version, plan_json, status)
           VALUES ('issue57', 'targeted_drill', '2026-09-06', 's', 'v', ?, 'completed')""",
        (json.dumps([{"question_id": qid}]),),
    )
    _attempt(conn, qid, correct=False, time_ms=50_000)
    conn.execute("UPDATE attempts SET session_id='issue57' WHERE question_id=?", (qid,))
    conn.commit()

    response = server_mod.feedback_reason("issue57", 0, "misread text", conn=conn)
    saved = conn.execute(
        "SELECT self_report_reason FROM attempts WHERE session_id='issue57'"
    ).fetchone()[0]

    assert response.status_code == 303
    assert saved == "misread text"


def test_benchmark_self_report_post_never_reveals_verdict(db):
    conn, _ = db
    wrong_qid = add_question(conn, source="custom_generated", pool="fresh_training", correct="B")
    correct_qid = add_question(conn, source="custom_generated", pool="fresh_training", correct="B")
    conn.execute(
        """INSERT INTO sessions (id, mode, created_at, seed, algo_version, plan_json, status)
           VALUES ('benchmark57', 'fresh_benchmark', '2026-09-06', 's', 'v', ?, 'open')""",
        (json.dumps([{"question_id": wrong_qid}, {"question_id": correct_qid}]),),
    )
    _attempt(conn, wrong_qid, correct=False, time_ms=50_000)
    _attempt(conn, correct_qid, correct=True, time_ms=60_000)
    conn.execute(
        "UPDATE attempts SET session_id='benchmark57' WHERE question_id IN (?, ?)",
        (wrong_qid, correct_qid),
    )
    conn.commit()

    wrong = server_mod.feedback_reason("benchmark57", 0, "misread text", conn=conn)
    correct = server_mod.feedback_reason("benchmark57", 1, "misread text", conn=conn)
    saved = conn.execute(
        "SELECT self_report_reason FROM attempts WHERE session_id='benchmark57' ORDER BY id"
    ).fetchall()

    assert wrong.status_code == correct.status_code == 303
    assert wrong.headers["location"] == "/question/benchmark57/1"
    assert correct.headers["location"] == "/question/benchmark57/2"
    assert [row[0] for row in saved] == ["", ""]


def test_report_rechecks_lease_after_render_before_publish(db, monkeypatch, tmp_path):
    conn, _ = db
    qid = add_question(conn, source="custom_generated", pool="fresh_training", correct="B")
    through = _attempt(conn, qid, correct=False, time_ms=50_000, tags=("over_inference",))
    conn.commit()
    lost = threading.Event()

    def render_and_lose_lease(*args, **kwargs):
        lost.set()
        return "stale report"

    monkeypatch.setattr(reports_mod, "render_report", render_and_lose_lease)
    try:
        reports_mod.generate_error_report(
            conn,
            after_attempt_id=0,
            through_attempt_id=through,
            out_name="lease-loss.html",
            reports_dir=tmp_path,
            abort_event=lost,
        )
    except RuntimeError as exc:
        assert "lease lost" in str(exc)
    else:
        raise AssertionError("lost lease should abort report publication")

    assert not (tmp_path / "lease-loss.html").exists()
