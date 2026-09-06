"""Aggregate coaching statistics and display-ready report context."""

import bisect
import collections
import datetime

from .report_coaching_analysis import analyze_rows, value
from .report_coaching_rules import RULES, coaching_rules

_TIME_BUCKETS = ("<60 sec", "1:00–1:45", "1:45–2:30", ">2:30")
_FAST_RULE = "Slow down and audit the winning choice before committing."
_SLOW_RULE = "Stop rereading; dumb-summary → predict → choose."


def _time_bucket(time_ms) -> str:
    try:
        seconds = int(time_ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return _TIME_BUCKETS[bisect.bisect_right((60, 105, 150), seconds)]


def _timing(rows: list[dict]) -> list[dict]:
    stats = {label: [0, 0] for label in _TIME_BUCKETS}
    for row in rows:
        label = _time_bucket(row.get("time_ms"))
        if label:
            stats[label][0] += 1
            stats[label][1] += int(not row.get("correct"))
    result = []
    for label, (attempts, wrong) in stats.items():
        coach = _FAST_RULE if label == _TIME_BUCKETS[0] else ""
        if label == _TIME_BUCKETS[-1]:
            coach = _SLOW_RULE
        result.append(
            {
                "label": label,
                "attempts": attempts,
                "wrong": wrong,
                "error_rate": round(100 * wrong / attempts, 1) if attempts else 0.0,
                "coach": coach,
            }
        )
    return result


def _example_label(row: dict) -> str:
    parts = (str(value(row, "source_test")), str(value(row, "source_question_number")))
    return " ".join(part for part in parts if part) or str(value(row, "official_skill", "question"))


def _behaviors(wrong: list[dict], counter: collections.Counter) -> list[dict]:
    result = []
    for label, count in counter.most_common(3):
        result.append(
            {
                "label": label,
                "count": count,
                "pct": round(100 * count / len(wrong)) if wrong else 0,
                "rule": RULES[label],
                "examples": [_example_label(row) for row in wrong if row.get("canonical_error") == label][:3],
            }
        )
    return result


def _display_time(raw) -> str:
    if not raw:
        return "unknown"
    try:
        stamp = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(raw)
    return stamp.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _time_label(time_ms) -> str:
    try:
        seconds = max(0, int(time_ms or 0)) // 1000
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _prepare_wrong(row: dict) -> dict:
    item = dict(row)
    item["example_label"] = _example_label(item)
    item["attempted_display"] = _display_time(item.get("attempted_at"))
    item["time_label"] = _time_label(item.get("time_ms"))
    item["passage_paragraphs"] = [part.strip() for part in str(item.get("passage") or "").split("\n\n") if part.strip()]
    item["rationale_paragraphs"] = [part.strip() for part in str(item.get("rationale") or "").split("\n\n") if part.strip()]
    chosen, correct = value(item, "chosen_letter"), value(item, "correct_letter")
    item["choices"] = [
        {**choice, "is_chosen": choice.get("letter") == chosen, "is_key": choice.get("letter") == correct}
        for choice in item.get("choices", [])
    ]
    return item


def _counts(rows: list[dict], key: str, default: str) -> list[tuple[str, int]]:
    return collections.Counter(value(row, key, default) for row in rows).most_common()


def build_context(conn, rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str) -> dict:
    analyzed = analyze_rows(conn, rows)
    wrong = [row for row in analyzed if not row.get("correct")]
    canonical = collections.Counter(row.get("canonical_error") for row in wrong if row.get("canonical_error"))
    correct_count = len(analyzed) - len(wrong)
    accuracy = round(100 * correct_count / len(analyzed), 1) if analyzed else 0.0
    return {
        "after_attempt_id": after_attempt_id,
        "through_attempt_id": through_attempt_id,
        "generated_display": _display_time(generated_at),
        "attempt_count": len(analyzed),
        "wrong_count": len(wrong),
        "accuracy": accuracy,
        "preventable": sum(row.get("prediction_preventable") == "yes" for row in wrong),
        "coaching_rules": coaching_rules(canonical),
        "behaviors": _behaviors(wrong, canonical),
        "domains": _counts(analyzed, "official_domain", "Unspecified"),
        "skills": _counts(analyzed, "official_skill", "Unspecified"),
        "modules": _counts(analyzed, "module", "Unspecified"),
        "confidence": collections.Counter(str(row.get("confidence")) if row.get("confidence") else "not recorded" for row in analyzed).most_common(),
        "timing": _timing(analyzed),
        "wrong": [_prepare_wrong(row) for row in wrong],
    }
