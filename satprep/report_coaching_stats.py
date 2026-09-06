"""Aggregate recurring error patterns and timing for coaching reports."""

import bisect
import collections

from .report_coaching_analysis import value
from .report_coaching_rules import RULES

TIME_BUCKETS = ("<60 sec", "1:00–1:45", "1:45–2:30", ">2:30")
_FAST_RULE = "Slow down and audit the winning choice before committing."
_SLOW_RULE = "Stop rereading; dumb-summary → predict → choose."


def _time_bucket(time_ms) -> str:
    try:
        seconds = int(time_ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return TIME_BUCKETS[bisect.bisect_right((60, 105, 150), seconds)]


def _coach(label: str) -> str:
    return {TIME_BUCKETS[0]: _FAST_RULE, TIME_BUCKETS[-1]: _SLOW_RULE}.get(label, "")


def timing(rows: list[dict]) -> list[dict]:
    stats = {label: [0, 0] for label in TIME_BUCKETS}
    for row in rows:
        label = _time_bucket(row.get("time_ms"))
        if not label:
            continue
        stats[label][0] += 1
        stats[label][1] += int(not row.get("correct"))
    return [
        {
            "label": label,
            "attempts": attempts,
            "wrong": wrong,
            "error_rate": round(100 * wrong / attempts, 1) if attempts else 0.0,
            "coach": _coach(label),
        }
        for label, (attempts, wrong) in stats.items()
    ]


def example_label(row: dict) -> str:
    parts = (str(value(row, "source_test")), str(value(row, "source_question_number")))
    return " ".join(part for part in parts if part) or str(value(row, "official_skill", "question"))


def behaviors(wrong: list[dict], counter: collections.Counter) -> list[dict]:
    return [
        {
            "label": label,
            "count": count,
            "pct": round(100 * count / len(wrong)) if wrong else 0,
            "rule": RULES[label],
            "examples": [example_label(row) for row in wrong if row.get("canonical_error") == label][:3],
        }
        for label, count in counter.most_common(3)
    ]


def counts(rows: list[dict], key: str, default: str) -> list[tuple[str, int]]:
    return collections.Counter(value(row, key, default) for row in rows).most_common()
