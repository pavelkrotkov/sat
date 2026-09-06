"""Build display-ready context for the coaching report template.

This layer only reshapes derived analysis for presentation. It does not persist
canonical mechanisms or mutate attempts; SQLite remains the source of truth.
"""

import collections
import datetime

from .report_coaching_analysis import analyze_rows, value
from .report_coaching_rules import coaching_rules
from .report_coaching_stats import behaviors, counts, example_label, timing


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


def _paragraphs(raw) -> list[str]:
    return [part.strip() for part in str(raw or "").split("\n\n") if part.strip()]


def _marked_choices(row: dict) -> list[dict]:
    chosen, correct = value(row, "chosen_letter"), value(row, "correct_letter")
    return [
        {**choice, "is_chosen": choice.get("letter") == chosen, "is_key": choice.get("letter") == correct}
        for choice in row.get("choices", [])
    ]


def _prepare_wrong(row: dict) -> dict:
    item = dict(row)
    item.update(
        example_label=example_label(item),
        attempted_display=_display_time(item.get("attempted_at")),
        time_label=_time_label(item.get("time_ms")),
        passage_paragraphs=_paragraphs(item.get("passage")),
        rationale_paragraphs=_paragraphs(item.get("rationale")),
        choices=_marked_choices(item),
    )
    return item


def _confidence(rows: list[dict]) -> list[tuple[str, int]]:
    labels = (str(row.get("confidence")) if row.get("confidence") else "not recorded" for row in rows)
    return collections.Counter(labels).most_common()


def _summary(analyzed: list[dict]) -> tuple[list[dict], collections.Counter, float, int]:
    wrong = [row for row in analyzed if not row.get("correct")]
    canonical = collections.Counter(row.get("canonical_error") for row in wrong if row.get("canonical_error"))
    accuracy = round(100 * (len(analyzed) - len(wrong)) / len(analyzed), 1) if analyzed else 0.0
    preventable = sum(row.get("prediction_preventable") == "yes" for row in wrong)
    return wrong, canonical, accuracy, preventable


def build_context(conn, rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str) -> dict:
    analyzed = analyze_rows(conn, rows)
    wrong, canonical, accuracy, preventable = _summary(analyzed)
    prepared_wrong = [_prepare_wrong(row) for row in wrong]
    return {
        "after_attempt_id": after_attempt_id,
        "through_attempt_id": through_attempt_id,
        "generated_display": _display_time(generated_at),
        "attempt_count": len(analyzed),
        "wrong_count": len(wrong),
        "accuracy": accuracy,
        "preventable": preventable,
        "coaching_rules": coaching_rules(canonical),
        "behaviors": behaviors(wrong, canonical),
        "domains": counts(analyzed, "official_domain", "Unspecified"),
        "skills": counts(analyzed, "official_skill", "Unspecified"),
        "modules": counts(analyzed, "module", "Unspecified"),
        "confidence": _confidence(analyzed),
        "timing": timing(analyzed),
        "wrong": prepared_wrong,
    }
