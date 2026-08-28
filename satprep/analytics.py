"""Analytics for the dashboard: patterns, not anecdotes (spec section 14).

Presentation only. "How risky is this skill/tag?" is answered by
satprep.weakness and read through `risk_scores`; this module shapes those
numbers for the dashboard and never computes a second opinion.
"""

from datetime import datetime, timedelta, timezone

from . import config
from .corpus.tags import tags_by_question
from .training.weakness import cached_profile, ensure_current, risk_scores


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


def recent_session_scores(conn, limit: int | None = 10, exclude: str | None = None,
                          before: tuple[str, int] | None = None) -> list[dict]:
    """Completed in-app sessions, most recently finished first, as accuracy.

    Bare counts on the results screen ("8/12") say nothing about whether that
    is a good day. Her own recent sessions are the only reference class that
    means anything here.

    Ordering is by when a session's work actually *finished* — `MAX(attempted_at)`
    over its attempts — not when it was created. A drill started on Monday and
    finished on Friday belongs after Tuesday's drill, and using creation order
    would let an older session that is still open slot itself behind results
    pages that were already rendered without it.

    `before` is a (finished_at, last_attempt_id) pair, restricting the set to
    sessions that finished earlier, so a results page reads the same whenever
    it is opened. Timestamps are second-resolution (`clock.utc_now`), so two
    drills finishing in the same second tie on the timestamp alone. The
    tie-break is the id of the last attempt — `attempts.id` is autoincrement,
    so it is the true order in which the work was finished. Breaking the tie on
    the session rowid instead would silently reintroduce *creation* order,
    which is the ordering this function exists to stop using.

    `limit=None` returns every retained finished session (Progress), while
    `full_dashboard` keeps its eight-item slice. A bare `LIMIT ?` bound to
    `NULL` is a sqlite3 type error in CPython, so the clause is appended only
    when a limit is actually wanted.
    """
    before_at, before_row = before if before else (None, None)
    sql = """SELECT s.id AS id, s.mode AS mode, s.created_at AS created_at,
                  MAX(a.attempted_at) AS finished_at,
                  MAX(a.id) AS last_attempt_id,
                  COUNT(a.id) AS n, COALESCE(SUM(a.correct), 0) AS c
           FROM sessions s
           JOIN attempts a ON a.session_id = s.id AND a.mode != 'historical'
           WHERE s.status = 'completed'
             AND (? IS NULL OR s.id != ?)
           GROUP BY s.id
           HAVING n > 0
              AND (? IS NULL
                   OR finished_at < ?
                   OR (finished_at = ? AND last_attempt_id < ?))
           ORDER BY finished_at DESC, last_attempt_id DESC"""
    params: list = [exclude, exclude, before_at, before_at, before_at, before_row]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r["id"], "mode": r["mode"], "created_at": r["created_at"],
         "finished_at": r["finished_at"],
         "n": r["n"], "correct": r["c"],
         "accuracy": round(100 * r["c"] / r["n"], 1)}
        for r in rows
    ]


def session_comparison(conn, session_id: str, summary: dict) -> dict:
    """This session's accuracy against the recent ones that finished before it.

    `delta` is None when there is no prior session to compare against, so the
    first drill reads as a baseline rather than an improvement of zero — and
    keeps reading that way when the page is opened again months later.
    """
    total = summary.get("total") or 0
    accuracy = round(100 * summary.get("correct", 0) / total, 1) if total else None
    anchor = conn.execute(
        """SELECT MAX(a.attempted_at) AS finished_at, MAX(a.id) AS last_attempt_id
           FROM attempts a
           WHERE a.session_id = ? AND a.mode != 'historical'""",
        (session_id,),
    ).fetchone()
    previous = recent_session_scores(
        conn, limit=5, exclude=session_id,
        before=((anchor["finished_at"], anchor["last_attempt_id"])
                if anchor and anchor["finished_at"] else None),
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


def practice_profile(conn, entity_type: str) -> list[dict]:
    """Weak entities as the student experiences them: every attempt counts.

    `skill_accuracy` and `tag_accuracy` filter to `a.mode='historical'` — they
    exist to describe the scraped Bluebook backlog. Driving a student-facing
    page from them means her own drills move the weakness score while the
    wrong/seen counts beside it never budge, and a profile built purely from
    in-app answers renders as "not enough data yet". This reads the same
    all-attempt model `/weaknesses` does.
    """
    profile = cached_profile(conn, entity_type)
    out = []
    for entity, stats in profile.items():
        wrong = stats.get("wrong", 0)
        correct = stats.get("correct", 0)
        seen = wrong + correct
        out.append({
            "name": entity,
            "risk_score": stats.get("score", 0.0),
            "wrong": wrong,
            "correct": correct,
            "seen": seen,
        })
    return sorted(out, key=lambda x: -x["risk_score"])


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
        # student-facing: counts every attempt, not just the historical import
        "practice_tags": practice_profile(conn, "tag"),
        "practice_skills": practice_profile(conn, "skill"),
    }
