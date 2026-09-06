"""Coaching analysis and rendering for incremental wrong-answer reports."""

from __future__ import annotations

import collections
import datetime
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import REPO_ROOT
from .explanations import explain_error
from .training.sessions import _logical_skeleton

_TIME_BUCKETS = ("<60 sec", "1:00–1:45", "1:45–2:30", ">2:30")
_KILL_WORD = re.compile(
    r"\b(always|never|only|most|primarily|primary|solely|all|none|must|proves?|causes?|rather than)\b",
    re.IGNORECASE,
)
_CANONICAL_GROUPS = {
    "Unsupported addition / over-inference": {
        "qualifier_strength",
        "over_inference",
        "absolute_vs_tentative_language",
        "unsupported_inference",
        "quantifier_mismatch",
        "scope_shift",
    },
    "Wrong relationship / direction": {
        "direction_reversal",
        "cause_vs_correlation",
        "wrong_reference_group",
        "comparison_relationship",
        "hypothesis_vs_result",
        "chronology",
    },
    "Failed to combine all evidence": {
        "failed_synthesis",
        "ignored_finding",
        "ignored_contrast",
        "incomplete_indirect_chain",
        "abstract_relationship_extraction",
    },
    "Missed governing constraint / keyword": {
        "contrast_concession",
        "logical_connector",
        "governing_constraint",
        "keyword",
    },
    "Right topic, wrong job / neighboring answer": {
        "true_but_not_supported",
        "same_topic_wrong_relationship",
        "irrelevant_detail",
        "main_claim_vs_detail",
        "evidence_relevance",
        "claim_vs_evidence",
    },
    "Literal factual misread": {
        "literal_misread",
        "misread_method",
        "misread_premise",
        "explicit_contradiction",
    },
    "Vocabulary / semantic precision": {
        "word_sense_in_context",
        "near_synonym_distinction",
        "paraphrase_precision",
        "collocation",
        "degree_or_intensity",
    },
}
_COACHING_RULES = {
    "Unsupported addition / over-inference": "Inference = minimum warranted conclusion. Audit every added actor, cause, comparison, and degree.",
    "Wrong relationship / direction": "Reduce the relationship to arrows before reading choices; preserve which variable does what.",
    "Failed to combine all evidence": "If the passage gives two findings, the answer must account for both.",
    "Missed governing constraint / keyword": "Circle the governing word—however, although, despite, together, indirect, rather than—and obey it.",
    "Right topic, wrong job / neighboring answer": "Ask what job the choice performs, not whether its topic appears in the passage.",
    "Literal factual misread": "Verify the exact premise or method against the text before inferring anything.",
    "Vocabulary / semantic precision": "Predict the sentence meaning first, then choose the word with the exact nuance and usage.",
}
_DEFAULT_RULES = (
    "Predict before looking at the choices; do not shop among four answers.",
    "Audit the strongest word in the final two choices.",
    "Use the minimum conclusion the text actually warrants.",
)
_PREDICTION_YES = {
    "Unsupported addition / over-inference",
    "Wrong relationship / direction",
    "Failed to combine all evidence",
    "Missed governing constraint / keyword",
    "Right topic, wrong job / neighboring answer",
}
_PREDICTION_NO = {"Literal factual misread", "Vocabulary / semantic precision"}
_FAST_RULE = "Slow down and audit the winning choice before committing."
_SLOW_RULE = "Stop rereading; dumb-summary → predict → choose."


def _value(row: dict, key: str, default=""):
    value = row.get(key)
    return default if value in (None, "") else value


def _json_list(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(value) for value in values] if isinstance(values, list) else []


def _choices(raw) -> list[dict]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


def _canonical_error(tags: list[str]) -> tuple[str, str]:
    for label, members in _CANONICAL_GROUPS.items():
        for tag in tags:
            if tag in members:
                return label, tag
    return "", tags[0] if tags else ""


def _prediction_preventable(canonical: str) -> str:
    if canonical in _PREDICTION_YES:
        return "yes"
    if canonical in _PREDICTION_NO:
        return "no"
    return "uncertain"


def _choice_text(choices: list[dict], letter: str) -> str:
    for choice in choices:
        if choice.get("letter") == letter:
            return str(choice.get("text") or "")
    return ""


def _kill_phrase(choices: list[dict], chosen_letter: str) -> str:
    match = _KILL_WORD.search(_choice_text(choices, chosen_letter))
    return match.group(0) if match else ""


def _distractor_bait(explanation) -> str:
    if explanation.mode == "llm" and explanation.tempting_answer:
        return explanation.tempting_answer
    return "The choice reuses passage concepts and looks text-grounded before its relationship is audited."


def _analyze_wrong(conn, row: dict) -> dict:
    item = dict(row)
    choices = _choices(item.get("choices_json"))
    explanation = explain_error(
        question_id=item["question_id"],
        passage=_value(item, "passage"),
        stem=_value(item, "stem"),
        choices=choices,
        student_letter=_value(item, "chosen_letter"),
        correct_letter=_value(item, "correct_letter"),
        rationale=_value(item, "rationale"),
        question_fingerprint=_value(item, "fingerprint"),
        conn=conn,
    )
    tags = _json_list(item.get("error_tags")) or list(explanation.error_taxonomy)
    canonical, subtype = _canonical_error(tags)
    item.update(
        canonical_error=canonical,
        error_subtype=subtype,
        dumb_summary=_logical_skeleton(_value(item, "passage"))[:2],
        prediction=explanation.correct_reasoning,
        distractor_bait=_distractor_bait(explanation),
        fatal_defect=explanation.exact_failure,
        kill_phrase=_kill_phrase(choices, _value(item, "chosen_letter")),
        reusable_rule=_COACHING_RULES.get(
            canonical, "Explain the exact defect in the chosen answer before moving on."
        ),
        prediction_preventable=_prediction_preventable(canonical),
        choices=choices,
    )
    return item


def _analyze_rows(conn, rows: list[dict]) -> list[dict]:
    return [_analyze_wrong(conn, row) if not row.get("correct") else dict(row) for row in rows]


def _time_bucket(time_ms) -> str:
    try:
        seconds = int(time_ms or 0) / 1000
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return _TIME_BUCKETS[0]
    if seconds < 105:
        return _TIME_BUCKETS[1]
    if seconds < 150:
        return _TIME_BUCKETS[2]
    return _TIME_BUCKETS[3]


def _timing(rows: list[dict]) -> list[dict]:
    stats = {label: [0, 0] for label in _TIME_BUCKETS}
    for row in rows:
        label = _time_bucket(row.get("time_ms"))
        if label:
            stats[label][0] += 1
            stats[label][1] += int(not row.get("correct"))
    return [
        {
            "label": label,
            "attempts": attempts,
            "wrong": wrong,
            "error_rate": round(100 * wrong / attempts, 1) if attempts else 0.0,
            "coach": _FAST_RULE
            if label == _TIME_BUCKETS[0]
            else _SLOW_RULE
            if label == _TIME_BUCKETS[3]
            else "",
        }
        for label, (attempts, wrong) in stats.items()
    ]


def _coaching_rules(counter: collections.Counter) -> list[str]:
    rules = [_COACHING_RULES[label] for label, _ in counter.most_common(5)]
    for rule in _DEFAULT_RULES:
        if rule not in rules:
            rules.append(rule)
        if len(rules) >= 3:
            break
    return rules[:5]


def _example_label(row: dict) -> str:
    source = str(_value(row, "source_test"))
    number = str(_value(row, "source_question_number"))
    return " ".join(part for part in (source, number) if part) or str(
        _value(row, "official_skill", "question")
    )


def _behaviors(wrong: list[dict], counter: collections.Counter) -> list[dict]:
    result = []
    for label, count in counter.most_common(3):
        result.append(
            {
                "label": label,
                "count": count,
                "pct": round(100 * count / len(wrong)) if wrong else 0,
                "rule": _COACHING_RULES[label],
                "examples": [
                    _example_label(row) for row in wrong if row.get("canonical_error") == label
                ][:3],
            }
        )
    return result


def _display_time(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def _time_label(time_ms) -> str:
    try:
        seconds = max(0, int(time_ms or 0)) // 1000
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _paragraphs(value) -> list[str]:
    return [part.strip() for part in str(value or "").split("\n\n") if part.strip()]


def _prepare_wrong(row: dict) -> dict:
    item = dict(row)
    item["example_label"] = _example_label(item)
    item["attempted_display"] = _display_time(item.get("attempted_at"))
    item["time_label"] = _time_label(item.get("time_ms"))
    item["passage_paragraphs"] = _paragraphs(item.get("passage"))
    item["rationale_paragraphs"] = _paragraphs(item.get("rationale"))
    chosen = _value(item, "chosen_letter")
    correct = _value(item, "correct_letter")
    item["choices"] = [
        {
            **choice,
            "is_chosen": choice.get("letter") == chosen,
            "is_key": choice.get("letter") == correct,
        }
        for choice in item.get("choices", [])
    ]
    return item


def _field_counts(rows: list[dict], key: str, default: str) -> list[tuple[str, int]]:
    return collections.Counter(_value(row, key, default) for row in rows).most_common()


def build_context(
    conn, rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str
) -> dict:
    analyzed = _analyze_rows(conn, rows)
    wrong = [row for row in analyzed if not row.get("correct")]
    canonical = collections.Counter(
        row.get("canonical_error") for row in wrong if row.get("canonical_error")
    )
    preventable = sum(row.get("prediction_preventable") == "yes" for row in wrong)
    return {
        "after_attempt_id": after_attempt_id,
        "through_attempt_id": through_attempt_id,
        "generated_display": _display_time(generated_at),
        "attempt_count": len(analyzed),
        "wrong_count": len(wrong),
        "accuracy": round(100 * (len(analyzed) - len(wrong)) / len(analyzed), 1)
        if analyzed
        else 0.0,
        "preventable": preventable,
        "coaching_rules": _coaching_rules(canonical),
        "behaviors": _behaviors(wrong, canonical),
        "domains": _field_counts(analyzed, "official_domain", "Unspecified"),
        "skills": _field_counts(analyzed, "official_skill", "Unspecified"),
        "modules": _field_counts(analyzed, "module", "Unspecified"),
        "confidence": collections.Counter(
            str(row.get("confidence")) if row.get("confidence") else "not recorded"
            for row in analyzed
        ).most_common(),
        "timing": _timing(analyzed),
        "wrong": [_prepare_wrong(row) for row in wrong],
    }


def render_report(
    conn, rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str
) -> str:
    env = Environment(
        loader=FileSystemLoader(Path(REPO_ROOT) / "satprep" / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("coaching_report.html")
    return template.render(
        **build_context(
            conn,
            rows,
            after_attempt_id=after_attempt_id,
            through_attempt_id=through_attempt_id,
            generated_at=generated_at,
        )
    )
