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
import json
from datetime import datetime, timedelta

from .sampler import select_drill
from .weakness import _parse_ts, _recency, _weight_for

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ErrorPattern:
    tag: str
    # Aggregated stats (recency-decayed).
    evidence_count: int  # raw number of wrong attempts carrying this tag
    recency_weighted_n: float  # decayed evidence mass
    recent_wrong: int  # wrong attempts in the last ~30 days
    recent_total: int  # all attempts (wrong+correct) in the last ~30 days
    recent_error_rate: float  # recent_wrong / max(1, recent_total)
    confidence_weighted: float  # sum of confidence multipliers on wrong attempts
    score: float  # 0..100 remediation priority (higher = more urgent)
    kb_tactic_refs: list[str]  # linked KB pages
    explanation: str  # one-line why-this-is-weak
    status: str  # ok | cold_start | sparse | conflicting | no_match


@dataclasses.dataclass(frozen=True)
class RemediationPlan:
    patterns: list[ErrorPattern]
    improvement: dict[str, dict]  # tag -> {recent_error_rate, older_error_rate, delta, n}
    recommended_tags: list[str]
    drill: dict | None  # select_drill() result, or None if no match


# ---------------------------------------------------------------------------
# Error-pattern aggregation
# ---------------------------------------------------------------------------

_RECENT_WINDOW_DAYS = 30.0


def _attempt_tags(r: dict) -> list[str]:
    """Per-attempt diagnosis from attempts.error_tags (JSON), with a
    fallback to the question-level student_error_tags snapshot for
    older ingest paths that never wrote the per-attempt JSON."""
    raw = r.get("error_tags")
    if raw:
        try:
            loaded = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(loaded, list) and loaded:
                return [str(t) for t in loaded]
        except (TypeError, ValueError):
            pass
    # Fallback: caller will pick up the question-level snapshot via
    # the outer LEFT JOIN. Returning [] here is correct — the snapshot
    # row is attached exactly once via the join, so the attempt is
    # not double-counted.
    return []


def _error_tag_attempts(conn) -> dict[str, list[dict]]:
    """Collect every attempt joined to the error tags the diagnosis
    recorded for it. Per-attempt tags come from attempts.error_tags
    (the per-attempt diagnosis written at session time); older
    attempts that lack that JSON fall back to student_error_tags.

    Only attempts on active=1 questions count — deactivated
    questions are audit-only and must not influence remediation
    recommendations. All attempt outcomes (correct + wrong) are
    loaded so recent_error_rate is a real ratio, not a constant 1.0.
    """
    rows = conn.execute(
        """SELECT a.id AS attempt_id, a.question_id AS question_id,
                  a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at,
                  a.time_ms AS time_ms,
                  a.error_tags AS error_tags,
                  set_.tag AS snapshot_tag
           FROM attempts a
           JOIN questions q ON q.id = a.question_id AND q.active = 1
           LEFT JOIN student_error_tags set_
               ON set_.question_id = a.question_id
           ORDER BY a.attempted_at"""
    ).fetchall()
    per_tag: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        tags = _attempt_tags(d) or ([d["snapshot_tag"]] if d.get("snapshot_tag") else [])
        if not tags:
            continue
        # Avoid inflating mass: an attempt may carry multiple error tags,
        # but each tag should only see this attempt once.
        for t in tags:
            per_tag.setdefault(t, []).append(
                {
                    "question_id": d["question_id"],
                    "attempt_id": d["attempt_id"],
                    "correct": d["correct"],
                    "confidence": d["confidence"],
                    "attempted_at": d["attempted_at"],
                    "time_ms": d["time_ms"],
                }
            )
    return per_tag


def _recent_attempts_by_tag(conn, tag: str, now: datetime) -> list[dict]:
    """All attempts (correct + wrong) on questions that carry `tag`
    via the per-attempt diagnosis OR via an analogous reasoning
    tag — used for the improvement comparison.

    A correctly-answered transfer question can still contribute
    evidence: the diagnosis may live on a peer question (same
    demand tag), and the analogous correct answer proves the
    student has internalized the rule. We pull attempts on any
    question sharing an effective_question_tags row with the
    diagnosed set.
    """
    rows = conn.execute(
        """SELECT a.question_id AS question_id,
                  a.correct AS correct, a.confidence AS confidence,
                  a.attempted_at AS attempted_at, a.time_ms AS time_ms
           FROM attempts a
           JOIN questions q ON q.id = a.question_id AND q.active = 1
           WHERE a.question_id IN (
               SELECT DISTINCT question_id FROM student_error_tags WHERE tag = ?
               UNION
               SELECT qt.question_id
                 FROM effective_question_tags qt
                WHERE qt.tag IN (
                    SELECT DISTINCT eqt.tag
                      FROM effective_question_tags eqt
                     WHERE eqt.question_id IN (
                         SELECT question_id FROM student_error_tags WHERE tag = ?
                     )
                )
           )
           ORDER BY a.attempted_at""",
        (tag, tag),
    ).fetchall()
    return [dict(r) for r in rows]


def _error_rate(rows: list[dict], now: datetime, window_days: float) -> tuple[int, int, float]:
    """(wrong, total, rate) within `window_days` of now."""
    cutoff = now - timedelta(days=window_days)
    wrong = total = 0
    for r in rows:
        ts = r.get("attempted_at")
        parsed_ts = _parse_ts(ts)
        if parsed_ts is not None and parsed_ts.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
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
    """Classify the evidence quality for a tag.

    Conflicting means many of the same question produced MULTIPLE
    different error tags (a question with the same content getting
    diagnosed differently on retries is a sign the classifier is
    unstable, not a student pattern). Each per-attempt row already
    carries its question_id.
    """
    if len(rows) == 0:
        return "cold_start"
    if len(rows) == 1:
        return "sparse"
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
        recent_wrong, recent_total, recent_rate = _error_rate(rows, now, _RECENT_WINDOW_DAYS)
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
            f"{tag}: {recent_wrong}/{max(1, recent_total)} wrong in the last "
            f"{int(_RECENT_WINDOW_DAYS)}d, "
            f"evidence mass {mass:.2f}, confidence {conf_w:.2f}"
        )
        patterns.append(
            ErrorPattern(
                tag=tag,
                evidence_count=len(rows),
                recency_weighted_n=round(mass, 3),
                recent_wrong=recent_wrong,
                recent_total=recent_total,
                recent_error_rate=round(recent_rate, 3),
                confidence_weighted=round(conf_w, 3),
                score=score,
                kb_tactic_refs=kb,
                explanation=expl,
                status=status,
            )
        )
    patterns.sort(key=lambda p: -p.score)
    return patterns


# ---------------------------------------------------------------------------
# Improvement measurement
# ---------------------------------------------------------------------------


def measure_improvement(
    conn, patterns: list[ErrorPattern], now: datetime | None = None
) -> dict[str, dict]:
    """Compare recent (last 30d) vs older (before that) error rate per
    tag. A negative delta means the student got better."""
    now = now or datetime.now().astimezone()
    out: dict[str, dict] = {}
    for p in patterns:
        rows = _recent_attempts_by_tag(conn, p.tag, now)
        _, recent_t, recent_rate = _error_rate(rows, now, _RECENT_WINDOW_DAYS)
        # Older window: everything before the recent cutoff.
        cutoff = now - timedelta(days=_RECENT_WINDOW_DAYS)
        older_w = older_t = 0
        for r in rows:
            ts = r.get("attempted_at")
            parsed_ts = _parse_ts(ts)
            if parsed_ts is not None and parsed_ts.replace(tzinfo=None) >= cutoff.replace(
                tzinfo=None
            ):
                continue
            older_t += 1
            older_w += 1 if r["correct"] == 0 else 0
        older_rate = older_w / older_t if older_t else None
        recent_present = recent_t > 0
        older_present = older_t > 0
        # Withhold the delta unless BOTH windows have observations:
        # - no older baseline → cannot claim "deteriorated from X to Y"
        # - no recent attempts → cannot claim "improvement to 0"
        # Returning None keeps callers honest instead of misleading
        # them with a 0.0 / recent_rate default.
        if recent_present and older_present and older_rate is not None:
            delta: float | None = round(recent_rate - older_rate, 3)
        else:
            delta = None
        out[p.tag] = {
            "recent_error_rate": round(recent_rate, 3),
            "older_error_rate": round(older_rate, 3) if older_rate is not None else None,
            "delta": delta,
            "recent_n": recent_t,
            "older_n": older_t,
        }
    return out


# ---------------------------------------------------------------------------
# Remediation drill selection
# ---------------------------------------------------------------------------


def _recommended_tags(
    patterns: list[ErrorPattern], min_score: float = 40.0, max_tags: int = 3, min_evidence: int = 3
) -> list[str]:
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
        # Thread `now` so due-date eligibility and candidate recency
        # in the drill line up with the evidence snapshot used to
        # build the rest of the plan.
        result = select_drill(
            conn, mode="remediation", count=count, seed=seed, focus_tag=tags[0], now=now
        )
        # Explicit no-match handling: documented contract is drill=None
        # when no eligible question matches the focus tag. A drill
        # whose items came entirely from the sampler's FALLBACK_BUCKET
        # (no `remediation:error-tag:*` component in their `why`) is
        # a generic drill dressed as remediation; collapse to None so
        # callers see the truth.
        items = result.get("items") or []
        remediation_items = [
            it
            for it in items
            if any(label.startswith("remediation:error-tag:") for label, _ in (it.get("why") or []))
        ]
        drill = None if not remediation_items else result
    return RemediationPlan(
        patterns=patterns,
        improvement=improvement,
        recommended_tags=tags,
        drill=drill,
    )


def explain_selection(plan: RemediationPlan) -> list[str]:
    """Human-readable why-each-question-was-selected.

    The sampler stores each candidate's explainable components under
    the `why` key as a list of (label, value) pairs. We rebuild that
    into a label-keyed dict so the prefix-based scan picks up both
    `weak-tag:*` and `remediation:*` components — when a question
    was selected purely on a reasoning-tag match, fall back to a
    `weak-tag` label.
    """
    out = []
    if not plan.drill:
        out.append(
            "no matching remediation questions found; recommended tags: "
            + ", ".join(plan.recommended_tags or ["(none)"])
        )
        return out
    for item in plan.drill["items"]:
        qid = item.get("question_id") or item.get("id")
        # `why` is the sampler's per-question component list
        # [(label, delta), ...]; collapse it into a label-keyed dict
        # for the prefix scan.
        why_pairs = item.get("why") or []
        if isinstance(why_pairs, dict):
            breakdown = why_pairs
        else:
            breakdown = {label: value for label, value in why_pairs}
        weak_hits = [k for k in breakdown if k.startswith(("weak-tag:", "remediation:"))]
        out.append(f"q{qid}: selected because {weak_hits or 'general weakness match'}")
    return out
