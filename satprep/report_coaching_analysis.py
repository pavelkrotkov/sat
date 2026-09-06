"""Per-miss coaching analysis for wrong-answer reports."""

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


def _json_list(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in values] if isinstance(values, list) else []


def _choices(raw) -> list[dict]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


def _choice_text(choices: list[dict], letter: str) -> str:
    for choice in choices:
        if choice.get("letter") == letter:
            return str(choice.get("text") or "")
    return ""


def _kill_phrase(choices: list[dict], letter: str) -> str:
    match = _KILL_WORD.search(_choice_text(choices, letter))
    return match.group(0) if match else ""


def _distractor_bait(explanation) -> str:
    if explanation.mode == "llm" and explanation.tempting_answer:
        return explanation.tempting_answer
    return "The choice reuses passage concepts and looks text-grounded before its relationship is audited."


def analyze_wrong(conn, row: dict) -> dict:
    item = dict(row)
    choices = _choices(item.get("choices_json"))
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
    tags = _json_list(item.get("error_tags")) or list(explanation.error_taxonomy)
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
