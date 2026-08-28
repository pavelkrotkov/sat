"""Analytics for the dashboard: patterns, not anecdotes (spec section 14).

Presentation only. "How risky is this skill/tag?" is answered by
satprep.weakness and read through `risk_scores`; this module shapes those
numbers for the dashboard and never computes a second opinion.
"""

from datetime import datetime, timedelta, timezone

from . import config
from .corpus.tags import tags_by_question
from .training.weakness import ensure_current, risk_scores


def skill_accuracy(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT q.official_skill AS skill,
                  COUNT(*) AS n, SUM(a.correct) AS c
           FROM attempts a JOIN questions q ON q.id=a.question_id
           WHERE a.mode='historical' AND q.active=1 AND q.official_skill != ''
           GROUP BY 1"""
    ).fetchall()
    model = risk_scores(conn, "skill", (r["skill"] for r in rows))
    out = []
    for r in rows:
        n, c = r["n"], r["c"] or 0
        out.append({
            "skill": r["skill"], "seen": n, "correct": c,
            "raw_accuracy": round(100 * c / n, 1),
            "risk_score": model.get(r["skill"], 0.0),
        })
    return sorted(out, key=lambda x: -x["risk_score"])


def tag_accuracy(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT qt.tag AS tag,
                  COUNT(DISTINCT a.id) AS n,
                  COALESCE(SUM(a.correct),0) AS c,
                  SUM(CASE WHEN a.correct=0 THEN 1 ELSE 0 END) AS w,
                  SUM(CASE WHEN a.correct=1 AND a.confidence<=2 THEN 1 ELSE 0 END) AS shaky_correct
           FROM effective_question_tags qt
           JOIN questions q ON q.id=qt.question_id AND q.active=1
           JOIN attempts a ON a.question_id=q.id AND a.mode='historical'
           GROUP BY 1"""
    ).fetchall()
    model = risk_scores(conn, "tag", (r["tag"] for r in rows if r["n"]))
    out = []
    for r in rows:
        n = r["n"]
        if not n:
            continue
        out.append({
            "tag": r["tag"], "seen": n, "correct": r["c"], "wrong": r["w"],
            "shaky_correct": r["shaky_correct"],
            "raw_accuracy": round(100 * r["c"] / n, 1),
            "risk_score": model.get(r["tag"], 0.0),
        })
    return sorted(out, key=lambda x: -x["risk_score"])


def high_value_misconceptions(conn) -> list[dict]:
    """wrong+confident / repeated wrong tags / slow-wrong / repeated 2-choice."""
    rows = conn.execute(
        """SELECT q.id, q.stem, q.official_skill,
                  a.chosen_letter, q.correct_letter, a.confidence, a.time_ms,
                  GROUP_CONCAT(DISTINCT et.tag) AS error_tags
           FROM attempts a
           JOIN questions q ON q.id=a.question_id
           LEFT JOIN student_error_tags et ON et.question_id=q.id
           WHERE a.correct=0 AND (a.mode != 'historical')
           GROUP BY a.id ORDER BY a.confidence DESC LIMIT 50"""
    ).fetchall()
    hist_rows = conn.execute(
        """SELECT q.id, q.stem, q.official_skill, a.chosen_letter, q.correct_letter,
                  NULL AS confidence, NULL AS time_ms, NULL AS error_tags
           FROM attempts a JOIN questions q ON q.id=a.question_id
           WHERE a.correct=0 AND a.mode='historical' LIMIT 200"""
    ).fetchall()
    out = []
    seen_ids = set()
    for r in rows + hist_rows:
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])
        out.append({
            "question_id": r["id"],
            "stem_excerpt": (r["stem"] or "")[:140],
            "skill": r["official_skill"],
            "chosen": r["chosen_letter"], "key": r["correct_letter"],
            "error_tags": (r["error_tags"] or "").split(",") if r["error_tags"] else [],
        })
    return out[:20]


def transfer_performance(conn) -> dict:
    """Separate accuracy: exact old items vs new items sharing weak tags vs benchmark."""
    weak_tag_names = {
        tag for tag, score in risk_scores(conn, "tag").items()
        if score >= config.WEAK_TAG_THRESHOLD
    }
    tag_map = tags_by_question(conn)

    rows = conn.execute(
        """SELECT a.correct AS c, a.question_id AS question_id,
                  CASE WHEN s.mode='fresh_benchmark' THEN 'benchmark_session'
                       ELSE '' END AS bench,
                  q.pool AS pool
           FROM attempts a
           JOIN sessions s ON s.id=a.session_id
           JOIN questions q ON q.id=a.question_id
           WHERE a.mode != 'historical'"""
    ).fetchall()
    buckets = {
        "old_exact": [0, 0],
        "new_same_weak_tag": [0, 0],
        "fresh_other": [0, 0],
        "protected_benchmark": [0, 0],
    }
    for r in rows:
        if r["bench"] == "benchmark_session":
            buckets["protected_benchmark"][0] += r["c"]
            buckets["protected_benchmark"][1] += 1
        elif r["pool"] == "historical":
            buckets["old_exact"][0] += r["c"]
            buckets["old_exact"][1] += 1
        elif any(t in weak_tag_names for t in tag_map.get(r["question_id"], []) or []):
            buckets["new_same_weak_tag"][0] += r["c"]
            buckets["new_same_weak_tag"][1] += 1
        else:
            buckets["fresh_other"][0] += r["c"]
            buckets["fresh_other"][1] += 1

    def pct(b):
        c, n = b
        return round(100 * c / n, 1) if n else None

    return {
        "old_exact_questions": {"n": buckets["old_exact"][1], "accuracy": pct(buckets["old_exact"])},
        "new_questions_sharing_weak_tags": {"n": buckets["new_same_weak_tag"][1], "accuracy": pct(buckets["new_same_weak_tag"])},
        "fresh_other": {"n": buckets["fresh_other"][1], "accuracy": pct(buckets["fresh_other"])},
        "protected_benchmark": {"n": buckets["protected_benchmark"][1], "accuracy": pct(buckets["protected_benchmark"])},
    }


def trend_by_tag(conn, window: int = 30) -> list[dict]:
    """Rolling recent performance by reasoning tag across in-app sessions."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
    rows = conn.execute(
        """SELECT qt.tag AS tag,
                  SUM(a.correct) AS c, COUNT(*) AS n
           FROM attempts a
           JOIN questions q ON q.id=a.question_id
           JOIN effective_question_tags qt ON qt.question_id=q.id
           WHERE a.attempted_at >= ? AND a.mode != 'historical'
           GROUP BY 1 HAVING n >= 2""",
        (cutoff,),
    ).fetchall()
    return [
        {"tag": r["tag"], "recent_n": r["n"], "recent_accuracy": round(100 * r["c"] / r["n"], 1)}
        for r in sorted(rows, key=lambda r: r["c"] / max(1, r["n"]))
    ]


def corpus_summary(conn) -> dict:
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    return {
        "questions_total": q("SELECT COUNT(*) FROM questions WHERE active=1"),
        "rw_historical": q("SELECT COUNT(*) FROM questions WHERE pool='historical'"),
        "fresh_training": q("SELECT COUNT(*) FROM questions WHERE pool='fresh_training'"),
        "protected_benchmark_unseen": q("SELECT COUNT(*) FROM questions WHERE pool='protected_benchmark' AND seen_benchmark=0"),
        "attempts_total": q("SELECT COUNT(*) FROM attempts"),
        "sessions_total": q("SELECT COUNT(*) FROM sessions WHERE status='completed'"),
    }


def recent_session_scores(conn, limit: int = 10, exclude: str | None = None,
                          before: tuple[str, int] | None = None) -> list[dict]:
    """Completed in-app sessions, newest first, as accuracy percentages.

    Bare counts on the results screen ("8/12") say nothing about whether that
    is a good day. Her own recent sessions are the only reference class that
    means anything here.

    `before` is a (created_at, rowid) pair restricting the set to sessions
    that precede it, so a results page reads the same whenever it is opened;
    without it, revisiting an old URL compares that session against drills
    that had not happened yet. Timestamps are second-resolution
    (`clock.utc_now`), so two drills in the same second tie on `created_at`
    alone — the rowid breaks that tie by insertion order.
    """
    before_at, before_row = before if before else (None, None)
    rows = conn.execute(
        """SELECT s.id AS id, s.mode AS mode, s.created_at AS created_at,
                  COUNT(a.id) AS n, COALESCE(SUM(a.correct), 0) AS c
           FROM sessions s
           JOIN attempts a ON a.session_id = s.id AND a.mode != 'historical'
           WHERE s.status = 'completed'
             AND (? IS NULL OR s.id != ?)
             AND (? IS NULL
                  OR s.created_at < ?
                  OR (s.created_at = ? AND s.rowid < ?))
           GROUP BY s.id
           HAVING n > 0
           ORDER BY s.created_at DESC, s.rowid DESC
           LIMIT ?""",
        (exclude, exclude, before_at, before_at, before_at, before_row, limit),
    ).fetchall()
    return [
        {"id": r["id"], "mode": r["mode"], "created_at": r["created_at"],
         "n": r["n"], "correct": r["c"],
         "accuracy": round(100 * r["c"] / r["n"], 1)}
        for r in rows
    ]


def session_comparison(conn, session_id: str, summary: dict) -> dict:
    """This session's accuracy against the recent ones that preceded it.

    `delta` is None when there is no prior session to compare against, so the
    first drill reads as a baseline rather than an improvement of zero — and
    keeps reading that way when the page is opened again months later.
    """
    total = summary.get("total") or 0
    accuracy = round(100 * summary.get("correct", 0) / total, 1) if total else None
    anchor = conn.execute(
        "SELECT created_at, rowid FROM sessions WHERE id=?", (session_id,)
    ).fetchone()
    previous = recent_session_scores(
        conn, limit=5, exclude=session_id,
        before=(anchor["created_at"], anchor["rowid"]) if anchor else None,
    )
    baseline = (round(sum(p["accuracy"] for p in previous) / len(previous), 1)
                if previous else None)
    delta = (round(accuracy - baseline, 1)
             if accuracy is not None and baseline is not None else None)
    best = max((p["accuracy"] for p in previous), default=None)
    return {
        "accuracy": accuracy,
        "baseline": baseline,
        "delta": delta,
        "previous": previous,
        "is_personal_best": (accuracy is not None and best is not None
                             and accuracy > best),
    }


def next_action(conn) -> dict:
    """What she should do next, which is what the dashboard should lead with.

    Corpus size is a maintenance statistic: it tells the operator the ingest
    worked and tells the student nothing she can act on.
    """
    weakest = risk_scores(conn, "tag")
    focus, score = "", 0.0
    if weakest:
        focus, score = max(weakest.items(), key=lambda kv: kv[1])
    last = conn.execute(
        """SELECT created_at FROM sessions
           WHERE status='completed' ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return {
        "focus_tag": focus,
        "focus_score": score,
        "last_session_at": last["created_at"] if last else None,
        "has_history": last is not None,
    }


def full_dashboard(conn) -> dict:
    # One model snapshot for the whole response: settle the cache before any
    # section reads it, or a lazy refresh partway through leaves the sections
    # above it on the previous model.
    ensure_current(conn)
    return {
        "corpus": corpus_summary(conn),
        "skills": skill_accuracy(conn),
        "tags": tag_accuracy(conn),
        "misconceptions": high_value_misconceptions(conn),
        "transfer": transfer_performance(conn),
        "recent_trend": trend_by_tag(conn),
        "next_action": next_action(conn),
        "recent_sessions": recent_session_scores(conn, limit=8),
    }
