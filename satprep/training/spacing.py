"""Adaptive spaced reinforcement (spec section 15).

SM-2-lite intervals in days. Exact-question repeats are deliberately rare:
the sampler prefers a *different* question sharing the reasoning tags unless
this item is actually due.
"""

from datetime import datetime, timedelta

INTERVALS_BY_OUTCOME = {
    # (correct, confidence): (multiplier applied to current interval, floor_days)
    (0, 3): (0.5, 0.5),   # confidently wrong: due again SOONEST
    (0, 2): (0.6, 1.0),
    (0, 1): (0.7, 1.5),   # ordinary gap
    (1, 1): (1.0, 1.0),   # correct-but-shaky: repeat earlier
    (1, 2): (1.6, 2.0),
    (1, 3): (2.4, 4.0),   # confident correct: grow substantially
}


def next_due(prev_interval_days: float, correct: int, confidence: int,
             now: datetime | None = None) -> tuple[float, str]:
    """Return (new_interval_days, due_at_iso)."""
    now = now or datetime.now().astimezone()
    mult, floor = INTERVALS_BY_OUTCOME.get((bool(correct), int(confidence or 1)), (1.0, 1.0))
    new_interval = max(floor, prev_interval_days * mult)
    # retire temporarily after several confident corrects
    if correct and confidence >= 3:
        new_interval = min(new_interval * 1.15, 120.0)
    else:
        new_interval = min(new_interval, 90.0)
    due_at = now + timedelta(days=new_interval)
    return round(new_interval, 2), due_at.isoformat()


def update_after_attempt(conn, question_id: int, correct: int, confidence: int,
                         now: datetime | None = None) -> float:
    """Update question_state after an in-app attempt; returns new interval."""
    now = now or datetime.now().astimezone()
    row = conn.execute(
        "SELECT interval_days FROM question_state WHERE question_id=?", (question_id,)
    ).fetchone()
    prev = row["interval_days"] if row else 1.0
    interval, due_at = next_due(prev, correct, confidence, now)
    conf = int(confidence or 0)
    conn.execute(
        """INSERT INTO question_state (question_id, times_seen, times_correct, times_wrong,
                                       confident_wrong_streak, interval_days, due_at, last_attempted_at)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(question_id) DO UPDATE SET
             times_seen = times_seen + 1,
             times_correct = times_correct + excluded.times_correct,
             times_wrong = times_wrong + excluded.times_wrong,
             confident_wrong_streak = CASE WHEN ?=0 AND ?>=3
                 THEN question_state.confident_wrong_streak + 1 ELSE 0 END,
             interval_days = excluded.interval_days,
             due_at = excluded.due_at,
             last_attempted_at = excluded.last_attempted_at""",
        (
            question_id,
            1 if correct else 0, 0 if correct else 1,
            1 if (not correct and conf >= 3) else 0,
            interval, due_at, now.isoformat(),
            int(bool(correct)), conf,
        ),
    )
    return interval


def is_due(state_row) -> bool:
    if state_row is None:
        return True
    try:
        due = state_row["due_at"]
    except (KeyError, TypeError, IndexError):
        due = getattr(state_row, "due_at", None)
    if not due:
        return False
    try:
        return datetime.fromisoformat(due) <= datetime.now().astimezone()
    except ValueError:
        return False
