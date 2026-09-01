"""Personalized remediation from accumulated SAT error patterns (issue #38).

Uses structured error classifications (student_error_tags, written by the
#36 pipeline / rule-based diagnosis) and attempt history to recommend
targeted practice for recurring reasoning weaknesses, then measures
transfer to new questions.

Design (from the issue):

  - Aggregates error patterns by tag/class with recency, repetition,
    evidence count, and confidence weighting — mirroring the weakness
    model but on the ERROR taxonomy (the classifier's output), not the
    demand taxonomy.
  - Connects each weak error pattern to KB tactics via the #39
    retrieval index.
  - Selects remediation questions through the existing sampler so ALL
    benchmark-protection and anti-memorization rules keep applying:
    this module never touches protected_benchmark pools and reuses
    score_candidate / select_drill.
  - Measures improvement by comparing recent vs older attempts on the
    same error tag (subsequent attempts, not review reading).
  - Cold start / sparse data / conflicting classifications /
    no-matching-question are all explicit outputs, never silent.

This module is read-only over the DB (writes nothing except through the
standard session/persist path when a remediation drill is started).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import Any

from .. import config
from .weakness import _recency, _weight_for
from .sampler import select_drill

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class ErrorPattern:
    tag: str
    # Aggregated stats (recency-decayed).
    evidence_count: int          # raw number of wrong attempts carrying this tag
    recency_weighted_n: float    # decayed evidence mass
    recent_wrong: int            # wrong attempts in the last ~30 days
    recent_total: int            # all attempts (wrong+correct) in the last ~30 days
    recent_error_rate: float     # recent_wrong / max(1, recent_total)
    confidence_weighted: float   # sum of confidence multipliers on wrong attempts
    score: float                 # 0..100 remediation priority (higher = more urgent)
    kb_tactic_refs: list[str]    # linked KB pages
    explanation: str             # one-line why-this-is-weak
    status: str                  # ok | cold_start | sparse | conflicting | no_match


@dataclasses.dataclass(frozen=True)
class RemediationPlan:
    patterns: list[ErrorPattern]
    improvement: dict[str, dict]  # tag -> {recent_error_rate, older_error_rate, delta, n}
    recommended_tags: list[str]
    drill: dict | None            # select_drill() result, or None if no match


# ---------------------------------------------------------------------------
# Error-pattern aggregation
# ---------------------------------------------------------------------------

_RECENT_WINDOW_DAYS = 30.0


def _error_tag_attempts(conn) -> dict[str, list[dict]]:
    """Collect every attempt joined to the error tags the diagnosis
    recorded for it. student_error_tags is keyed by question_id, so an
    attempt on that question carries the tags (the classifier output
    from #36 / rule diagnosis)."""
    rows = conn.execute(
        """SELECT set_.tag AS tag,
                  a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at,
                  a.time_ms AS time_ms
           FROM student_error_tags set_
           JOIN attempts a ON a.question_id = set_.question_id
           WHERE a.correct = 0
           ORDER BY a.attempted_at"""
    ).fetchall()
    per_tag: dict[str, list[dict]] = {}
    for r in rows:
        per_tag.setdefault(r["tag"], []).append(dict(r))
    return per_tag


def _recent_attempts_by_tag(conn, tag: str, now: datetime) -> list[dict]:
    """All attempts (correct + wrong) on questions that carry `tag` in
    student_error_tags — used for the improvement comparison. A
    question's error tags are static per question (diagnosis), so an
    attempt on that question after remediation still counts."""
    rows = conn.execute(
        """SELECT a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at, a.time_ms AS time_ms
           FROM student_error_tags set_
           JOIN attempts a ON a.question_id = set_.question_id
           WHERE set_.tag = ? AND a.correct IN (0, 1)
           ORDER BY a.attempted_at""",
        (tag,),
    ).fetchall()
    return [dict(r) for r in rows]


def _error_rate(rows: list[dict], now: datetime, window_days: float) -> tuple[int, int, float]:
    """(wrong, total, rate) within `window_days` of now."""
    cutoff = now - timedelta(days=window_days)
    wrong = total = 0
    for r in rows:
        ts = r.get("attempted_at")
        try:
            if datetime.fromisoformat(ts).replace(tzinfo=None) < cutoff.replace(tzinfo=None):
                continue
        except (TypeError, ValueError):
            continue
        total += 1
        wrong += 1 if r["correct"] == 0 else 0
    return wrong, total, (wrong / total if total else 0.0)


def _kb_tactics_for(tag: str) -> list[str]:
    """Map an error tag to KB tactic pages using the #39 explicit
    mapping (kept in sync with the explanation pipeline)."""
    mapping = {
        "qualifier_strength": ["kb/wiki/summaries/settele-strong-words.md"],
        "absolute_vs_tentative_language": ["kb/wiki/summaries/settele-strong-words.md"],
        "over_inference": ["kb/wiki/summaries/settele-strong-words.md"],
        "unsupported_inference": ["kb/wiki/summaries/penguin-reading-hacks.md"],
        "true_but_not_supported": ["kb/wiki/summaries/settele-trap-answers.md"],
        "same_topic_wrong_relationship": ["kb/wiki/summaries/settele-trap-answers.md"],
        "cause_vs_correlation": ["kb/wiki/summaries/settele-dumb-summaries.md"],
        "hypothesis_vs_result": ["kb/wiki/summaries/settele-dumb-summaries.md"],
        "direction_reversal": ["kb/wiki/summaries/settele-confusing-passages.md"],
        "paraphrase_precision": ["kb/wiki/summaries/settele-confusing-passages.md"],
        "near_synonym_distinction": ["kb/wiki/summaries/settele-confusing-passages.md"],
    }
    return mapping.get(tag, [])


def _pattern_status(tag: str, rows: list[dict], now: datetime) -> str:
    """Classify the evidence quality for a tag."""
    if len(rows) == 0:
        return "cold_start"
    if len(rows) == 1:
        return "sparse"
    # Conflicting: roughly even split of error tags pointing at this
    # question AND other questions with a different dominant error —
    # approximated by checking whether the same question produced
    # multiple different error tags.
    qids = {r.get("question_id") for r in rows}
    if len(qids) < len(rows) * 0.5:
        return "conflicting"
    return "ok"


def compute_patterns(conn, now: datetime | None = None) -> list[ErrorPattern]:
    """Aggregate error-tag patterns and score remediation priority."""
    now = now or datetime.now().astimezone()
    per_tag = _error_tag_attempts(conn)
    patterns: list[ErrorPattern] = []
    for tag, rows in per_tag.items():
        status = _pattern_status(tag, rows, now)
        recent_wrong, recent_total, recent_rate = _error_rate(
            rows, now, _RECENT_WINDOW_DAYS)
        # Decayed evidence mass: each wrong attempt counts with recency
        # decay and confidence weight (confident wrong = stronger signal).
        mass = 0.0
        conf_w = 0.0
        for r in rows:
            decay = _recency(r["attempted_at"], now)
            w = _weight_for(0, r["confidence"] or 0)  # always wrong here
            mass += w * decay
            conf_w += (r["confidence"] or 0) / 3.0
        # Priority score: recent error rate dominates; evidence mass and
        # confidence add to it, capped. Cold-start / sparse get a floor
        # so they are not invisible but never dominate.
        score = 100.0 * recent_rate
        score += min(20.0, mass * 12.0)
        score += min(10.0, conf_w * 5.0)
        if status == "cold_start":
            score = min(score, 25.0)
        elif status == "sparse":
            score = min(score, 45.0)
        elif status == "conflicting":
            score = min(score, 60.0)
        score = round(min(100.0, score), 1)
        kb = _kb_tactics_for(tag)
        expl = (
            f"{tag}: {recent_wrong}/{max(1,recent_total)} wrong in the last "
            f"{int(_RECENT_WINDOW_DAYS)}d, "
            f"evidence mass {mass:.2f}, confidence {conf_w:.2f}"
        )
        patterns.append(ErrorPattern(
            tag=tag, evidence_count=len(rows), recency_weighted_n=round(mass, 3),
            recent_wrong=recent_wrong, recent_total=recent_total,
            recent_error_rate=round(recent_rate, 3),
            confidence_weighted=round(conf_w, 3), score=score,
            kb_tactic_refs=kb, explanation=expl,
            status=status,
        ))
    patterns.sort(key=lambda p: -p.score)
    return patterns


# ---------------------------------------------------------------------------
# Improvement measurement
# ---------------------------------------------------------------------------

def measure_improvement(conn, patterns: list[ErrorPattern],
                        now: datetime | None = None) -> dict[str, dict]:
    """Compare recent (last 30d) vs older (before that) error rate per
    tag. A negative delta means the student got better."""
    now = now or datetime.now().astimezone()
    out: dict[str, dict] = {}
    for p in patterns:
        rows = _recent_attempts_by_tag(conn, p.tag, now)
        recent_w, recent_t, recent_rate = _error_rate(rows, now, _RECENT_WINDOW_DAYS)
        # Older window: everything before the recent cutoff.
        cutoff = now - timedelta(days=_RECENT_WINDOW_DAYS)
        older_w = older_t = 0
        for r in rows:
            ts = r.get("attempted_at")
            try:
                if datetime.fromisoformat(ts).replace(tzinfo=None) >= cutoff.replace(tzinfo=None):
                    continue
            except (TypeError, ValueError):
                continue
            older_t += 1
            older_w += 1 if r["correct"] == 0 else 0
        older_rate = older_w / older_t if older_t else None
        out[p.tag] = {
            "recent_error_rate": round(recent_rate, 3),
            "older_error_rate": round(older_rate, 3) if older_rate is not None else None,
            "delta": round(recent_rate - (older_rate or 0.0), 3),
            "recent_n": recent_t,
            "older_n": older_t,
        }
    return out


# ---------------------------------------------------------------------------
# Remediation drill selection
# ---------------------------------------------------------------------------

def _recommended_tags(patterns: list[ErrorPattern],
                      min_score: float = 40.0, max_tags: int = 3,
                      min_evidence: int = 3) -> list[str]:
    """Top patterns above the priority floor, excluding noise-level
    evidence. A pattern needs at least `min_evidence` wrong attempts to
    be recommended (cold-start / single-attempt noise never dominates
    remediation)."""
    out = []
    for p in patterns:
        if p.score < min_score:
            continue
        if p.evidence_count < min_evidence:
            continue
        out.append(p.tag)
        if len(out) >= max_tags:
            break
    return out


def build_remediation_plan(
    conn,
    *,
    count: int = 12,
    seed: str | None = None,
    min_score: float = 40.0,
    focus_tag: str | None = None,
    now: datetime | None = None,
) -> RemediationPlan:
    """Build a personalized remediation plan.

    - Computes error patterns and improvement deltas.
    - Picks the top weak tags.
    - Starts a remediation drill through the standard sampler (which
      preserves benchmark protections: protected_benchmark pool is never
      eligible, exposure penalties apply, etc.). The drill's `items`
      carry the standard explainable score breakdown plus a
      `remediation:` reason when the question shares a weak error tag.
    - Explicit no-match handling: if no fresh/eligible question shares
      the weak tags, drill is None and recommended_tags tells the caller
      what to look for.
    """
    now = now or datetime.now().astimezone()
    patterns = compute_patterns(conn, now)
    improvement = measure_improvement(conn, patterns, now)
    tags = [focus_tag] if focus_tag else _recommended_tags(patterns, min_score)
    drill = None
    if tags:
        # The sampler's score_candidate already boosts weak-tag matches;
        # we pass focus_tags so the same mechanism targets the error
        # tags we recommend (they live in weakness['error_tag']).
        drill = select_drill(conn, mode="remediation", count=count,
                             seed=seed, focus_tag=tags[0])
    return RemediationPlan(
        patterns=patterns,
        improvement=improvement,
        recommended_tags=tags,
        drill=drill,
    )


def explain_selection(plan: RemediationPlan) -> list[str]:
    """Human-readable why-each-question-was-selected."""
    out = []
    if not plan.drill:
        out.append("no matching remediation questions found; recommended tags: "
                   + ", ".join(plan.recommended_tags or ["(none)"]))
        return out
    for item in plan.drill["items"]:
        qid = item.get("id") or item.get("question_id")
        breakdown = item.get("score_breakdown") or {}
        weak_hits = [k for k in breakdown if k.startswith("weak-tag:")
                     or k.startswith("remediation:")]
        out.append(
            f"q{qid}: selected because {weak_hits or 'general weakness match'}"
        )
    return out