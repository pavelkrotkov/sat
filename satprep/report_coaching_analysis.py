"""Per-miss coaching analysis for wrong-answer reports.

The persisted attempt tags remain authoritative. Explanation output supplies
card-level coaching detail and is only a fallback when an attempt has no tags.
"""

import json
import re

from .explanations import explain_error
from .report_coaching_rules import canonical_error, prediction_preventable, rule_for
from .training.sessions import _logical_skeleton

_KILL_WORD = re.compile(
    r"\b(always|never|only|most|primarily|primary|solely|all|none|must|proves?|causes?|rather than)\b",
    re.IGNORECASE,
)


def value(row: dict, key: str, default=""):
    found = row.get(key)
    return default if found in (None, "") else found


def _json_array(raw) -> list:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


def _kill_phrase(choices: list[dict], letter: str) -> str:
    text = next(
        (str(choice.get("text") or "") for choice in choices if choice.get("letter") == letter),
        "",
    )
    match = _KILL_WORD.search(text)
    return match.group(0) if match else ""


def _distractor_bait(explanation) -> str:
    if explanation.mode == "llm" and explanation.tempting_answer:
        return explanation.tempting_answer
    return "The choice reuses passage concepts and looks text-grounded before its relationship is audited."


def analyze_wrong(conn, row: dict) -> dict:
    item = dict(row)
    choices = _json_array(item.get("choices_json"))
    explanation = explain_error(
        question_id=item["question_id"],
        passage=value(item, "passage"),
        stem=value(item, "stem"),
        choices=choices,
        student_letter=value(item, "chosen_letter"),
        correct_letter=value(item, "correct_letter"),
        rationale=value(item, "rationale"),
        question_fingerprint=value(item, "fingerprint"),
        conn=conn,
    )
    tags = [str(tag) for tag in _json_array(item.get("error_tags"))] or list(
        explanation.error_taxonomy
    )
    canonical, subtype = canonical_error(tags)
    item.update(
        canonical_error=canonical,
        error_subtype=subtype,
        dumb_summary=_logical_skeleton(value(item, "passage"))[:2],
        prediction=explanation.correct_reasoning,
        distractor_bait=_distractor_bait(explanation),
        fatal_defect=explanation.exact_failure,
        kill_phrase=_kill_phrase(choices, value(item, "chosen_letter")),
        reusable_rule=rule_for(canonical),
        prediction_preventable=prediction_preventable(canonical),
        choices=choices,
    )
    return item


def analyze_rows(conn, rows: list[dict]) -> list[dict]:
    return [analyze_wrong(conn, row) if not row.get("correct") else dict(row) for row in rows]
