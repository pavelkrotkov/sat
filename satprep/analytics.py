"""Analytics for the dashboard: patterns, not anecdotes (spec section 14)."""

import json
from datetime import datetime, timedelta

from .db import connect


def _smoothed_rate(wrong: float, n: float, k: float = 6.0, prior: float = 0.25) -> float:
    if n <= 0:
        return 0.0
    return round(100.0 * (wrong + k * prior) / (n + k))


def _model_scores(conn, entity_type: str) -> dict[str, float]:
    return {
        r["entity"]: r["score"]
        for r in conn.execute(
            "SELECT entity, score FROM weakness_cache WHERE entity_type=?", (entity_type,)
        ).fetchall()
    }


def skill_accuracy(conn) -> list[dict]:
    model = _model_scores(conn, "skill")
    rows = conn.execute(
        """SELECT q.official_skill AS skill,
                  COUNT(*) AS n, SUM(a.correct) AS c
           FROM attempts a JOIN questions q ON q.id=a.question_id
           WHERE a.mode='historical' AND q.active=1 AND q.official_skill != ''
           GROUP BY 1"""
    ).fetchall()
    out = []
    for r in rows:
        n, c = r["n"], r["c"] or 0
        out.append({
            "skill": r["skill"], "seen": n, "correct": c,
            "raw_accuracy": round(100 * c / n, 1),
            # prefer the full weakness-model score; fall back to raw smoothing
            "risk_score": model.get(r["skill"]) or _smoothed_rate(n - c, n),
        })
    return sorted(out, key=lambda x: -x["risk_score"])


def tag_accuracy(conn) -> list[dict]:
    model = _model_scores(conn, "tag")
    rows = conn.execute(
        """SELECT qt.tag AS tag,
                  COUNT(DISTINCT a.id) AS n,
                  COALESCE(SUM(a.correct),0) AS c,
                  SUM(CASE WHEN a.correct=0 THEN 1 ELSE 0 END) AS w,
                  SUM(CASE WHEN a.correct=1 AND a.confidence<=2 THEN 1 ELSE 0 END) AS shaky_correct
           FROM question_tags qt
           JOIN questions q ON q.id=qt.question_id AND q.active=1
           JOIN attempts a ON a.question_id=q.id AND a.mode='historical'
           GROUP BY 1"""
    ).fetchall()
    out = []
    for r in rows:
        n = r["n"]
        if not n:
            continue
        out.append({
            "tag": r["tag"], "seen": n, "correct": r["c"], "wrong": r["w"],
            "shaky_correct": r["shaky_correct"],
            "raw_accuracy": round(100 * r["c"] / n, 1),
            "risk_score": model.get(r["tag"]) or _smoothed_rate(r["w"], n),
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
    weak_tags_rows = conn.execute(
        "SELECT entity FROM weakness_cache WHERE entity_type='tag' AND score>=55"
    ).fetchall()
    weak_tag_names = {r["entity"] for r in weak_tags_rows}
    tag_map: dict[int, list[str]] = {}
    for r in conn.execute("SELECT question_id, tag FROM question_tags"):
        tag_map.setdefault(r["question_id"], []).append(r["tag"])

    rows = conn.execute(
        """SELECT a.correct AS c, q.pool AS pool FROM attempts a
           JOIN questions q ON q.id=a.question_id WHERE a.mode != 'historical'"""
    ).fetchall()
    buckets = {
        "old_exact": [0, 0],
        "new_same_weak_tag": [0, 0],
        "fresh_other": [0, 0],
        "protected_benchmark": [0, 0],
    }
    for r in rows:
        if r["pool"] == "historical":
            buckets["old_exact"][0] += r["c"]
            buckets["old_exact"][1] += 1
        elif r["pool"] == "protected_benchmark":
            buckets["protected_benchmark"][0] += r["c"]
            buckets["protected_benchmark"][1] += 1
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
    cutoff = (datetime.now().astimezone() - timedelta(days=window)).isoformat()
    rows = conn.execute(
        """SELECT qt.tag AS tag,
                  SUM(a.correct) AS c, COUNT(*) AS n
           FROM attempts a
           JOIN questions q ON q.id=a.question_id
           JOIN question_tags qt ON qt.question_id=q.id
           WHERE a.attempted_at >= ? AND a.mode != 'historical'
           GROUP BY 1 HAVING n >= 2""",
        (cutoff,),
    ).fetchall()
    return [
        {"tag": r["tag"], "recent_n": r["n"], "recent_accuracy": round(100 * r["c"] / r["n"], 1)}
        for r in sorted(rows, key=lambda r: r["c"] / max(1, r["n"]))
    ]


def corpus_summary(db_path=None) -> dict:
    conn = connect(db_path)
    q = lambda s: conn.execute(s).fetchone()[0]  # noqa: E731
    summary = {
        "questions_total": q("SELECT COUNT(*) FROM questions WHERE active=1"),
        "rw_historical": q("SELECT COUNT(*) FROM questions WHERE pool='historical'"),
        "fresh_training": q("SELECT COUNT(*) FROM questions WHERE pool='fresh_training'"),
        "protected_benchmark_unseen": q("SELECT COUNT(*) FROM questions WHERE pool='protected_benchmark' AND seen_benchmark=0"),
        "attempts_total": q("SELECT COUNT(*) FROM attempts"),
        "sessions_total": q("SELECT COUNT(*) FROM sessions WHERE status='completed'"),
    }
    conn.close()
    return summary


def full_dashboard(db_path=None) -> dict:
    conn = connect(db_path)
    data = {
        "corpus": corpus_summary(db_path=db_path),
        "skills": skill_accuracy(conn),
        "tags": tag_accuracy(conn),
        "misconceptions": high_value_misconceptions(conn),
        "transfer": transfer_performance(conn),
        "recent_trend": trend_by_tag(conn),
    }
    conn.close()
    return data
