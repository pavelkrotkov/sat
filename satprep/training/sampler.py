"""Explainable weighted question sampler (spec sections 9, 10, 11, 18, 21).

Every candidate receives an additive score whose components are stored in
the session plan so the admin UI can answer: why was this question selected?

Protected benchmark questions are excluded by FILTERING at the query level
in every non-benchmark mode; they are never reachable through weights.
"""

import json
import random
from datetime import datetime

from .. import ALGO_VERSION, config
from ..clock import utc_now
from ..corpus.questions import iter_active
from ..corpus.tags import tags_by_question
from ..ids import session_id
from .candidates import Candidate, row_field
from .composition import MODE_COMPOSITIONS, compose, pools_for
from .spacing import is_due
from .weakness import cached_profile, compute_weakness


def _load_candidates(conn, include_pools: tuple[str, ...]) -> list[Candidate]:
    # The pool filter stays in SQL: a protected benchmark item is never even
    # loaded outside benchmark mode, so no amount of scoring can surface one.
    questions = list(iter_active(conn, pools=include_pools, displayable_only=True))
    tag_map = tags_by_question(conn)
    state_map = {r["question_id"]: r for r in conn.execute("SELECT * FROM question_state")}
    hist_map: dict[int, int] = {}
    for r in conn.execute(
        "SELECT question_id, MAX(correct) AS c FROM attempts WHERE mode='historical' GROUP BY question_id"
    ):
        hist_map[r["question_id"]] = r["c"]
    out = []
    for question in questions:
        c = Candidate(question, tag_map.get(question.id, []))
        c.state = state_map.get(question.id)
        c.hist_correct = hist_map.get(question.id)
        out.append(c)
    return out


def _matched_weak_tags(cand: Candidate, weak_tags: dict) -> list[str]:
    matched = [tag for tag in cand.tags if tag in weak_tags]
    matched.sort(key=lambda tag: -weak_tags[tag]["score"])
    return matched


def _score_weak_tags(
    cand: Candidate, weak_tags: dict, matched: list[str], focus_tags: list[str] | None
) -> None:
    if not matched:
        return
    top = matched[0]
    bonus = config.W_WEAK_TAG_MATCH * weak_tags[top]["score"] / 100.0
    if focus_tags and top in focus_tags:
        bonus *= 1.35
    cand.add(f"weak-tag:{top}", bonus)
    if len(matched) > 1:
        second = matched[1]
        cand.add(
            f"weak-tag-2nd:{second}",
            config.W_WEAK_SECONDARY_TAG * (weak_tags[second]["score"] / 100.0),
        )


def _score_error_tags(cand: Candidate, weakness: dict, focus_tags: list[str] | None) -> None:
    for tag, payload in weakness.get("error_tag", {}).items():
        score = payload.get("score", 0) or 0
        question_ids = payload.get("question_ids") or []
        if score <= 0 or not question_ids or cand.question.id not in question_ids:
            continue
        bonus = config.W_WEAK_TAG_MATCH * (score / 100.0)
        if focus_tags and tag in focus_tags:
            bonus *= 1.35
        cand.add(f"remediation:error-tag:{tag}", bonus)


def _score_content(cand: Candidate, weakness: dict) -> None:
    skill = cand.question.official_skill
    weak_skills = weakness.get("skill", {})
    if skill and skill in weak_skills:
        cand.add(
            f"skill-weakness:{skill}", config.W_SKILL_WEAKNESS * weak_skills[skill]["score"] / 100.0
        )
    if skill in config.SEMANTIC_SKILLS:
        cand.add("semantic-content-bias", config.W_SEMANTIC_BIAS)
    difficulty = cand.question.difficulty
    diff_bonus = {"hard": config.W_HARD_DIFFICULTY, "medium": 0.5}.get(difficulty, 0.7)
    cand.add("difficulty" + (f":{difficulty}" if difficulty else ":unknown"), diff_bonus)


def _score_pool_state(cand: Candidate, weakness: dict, matched: list[str], due_now: bool) -> None:
    question = cand.question
    hist_correct = cand.hist_correct if question.pool == "historical" else False
    weak_skills = weakness.get("skill", {})
    if question.pool == "fresh_training":
        if matched:
            cand.add("fresh-matching-weak-tags", config.W_FRESH_MATCHING_WEAK)
        elif question.official_skill and question.official_skill in weak_skills:
            cand.add("fresh-neighbor-skill", config.W_FRESH_NEIGHBOR)
    if question.pool == "historical" and due_now and hist_correct == 0:
        cand.add("due-for-review-previously-wrong", config.W_DUE_INCORRECT)
    if question.pool == "historical" and hist_correct == 1 and matched:
        cand.add("transfer-correct-shares-weak-tag", config.W_TRANSFER_CORRECT)


def _score_exposure(cand: Candidate, state) -> None:
    seen_times = row_field(state, "times_seen", 0)
    exposure_penalty = min(
        config.PENALTY_EXPOSURE_CAP, config.PENALTY_EXPOSURE_PER_SEEN * seen_times
    )
    if exposure_penalty:
        cand.add("exposure-penalty", -exposure_penalty)
    if (
        row_field(state, "times_correct", 0) >= 2
        and row_field(state, "confident_wrong_streak", 0) == 0
    ):
        cand.add("recently-mastered-penalty", -config.PENALTY_RECENT_MASTERED)


def _score_recency(cand: Candidate, state, now: datetime) -> None:
    last_attempted = row_field(state, "last_attempted_at", None)
    if not last_attempted:
        return
    try:
        days_since = (now - datetime.fromisoformat(last_attempted)).days
    except ValueError:
        return
    if days_since >= 45:
        cand.add(f"not-seen-in-{min(days_since, 999)}-days", config.W_NOT_SEEN_LONG_AGO)


def _score_state(
    cand: Candidate,
    weakness: dict,
    matched: list[str],
    now: datetime,
) -> None:
    question = cand.question
    state = cand.state
    hist_correct = cand.hist_correct if question.pool == "historical" else False
    due_now = is_due(state, now) or (question.pool == "historical" and hist_correct == 0)
    _score_pool_state(cand, weakness, matched, due_now)
    _score_exposure(cand, state)
    _score_recency(cand, state, now)


def score_candidate(
    cand: Candidate,
    weakness: dict,
    focus_tags: list[str] | None = None,
    now: datetime | None = None,
) -> Candidate:
    """Additive explainable score (mirrors config weights)."""
    now = now or datetime.now().astimezone()
    weak_tags = weakness.get("tag", {})
    matched = _matched_weak_tags(cand, weak_tags)
    cand.add("base", 0.5)
    _score_weak_tags(cand, weak_tags, matched, focus_tags)
    _score_error_tags(cand, weakness, focus_tags)
    _score_content(cand, weakness)
    _score_state(cand, weakness, matched, now)
    return cand


def select_drill(
    conn,
    mode: str,
    count: int | None = None,
    seed: str | None = None,
    focus_tag: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Build a drill plan. Returns {'session_id', 'items': [...], 'seed'}.

    items: [{'question_id', 'weight', 'why': [(component, delta)], 'bucket'}]
    Protected benchmark leakage is structurally impossible outside
    fresh_benchmark mode because candidates are filtered by pool.

    `now` is the reference time for due-date eligibility and
    candidate recency. Defaults to wall-clock when omitted, but
    callers that have already captured a timestamp for
    reproducibility (e.g. remediation plans) should pass it through
    so the drill is consistent with the rest of the plan.
    """
    seed = seed or utc_now()
    rng = random.Random(seed)
    now = now or datetime.now().astimezone()

    weakness = cached_profile(conn)
    if not weakness.get("tag") and not weakness.get("skill"):
        weakness = compute_weakness(conn, now=now)

    focus_tags = [focus_tag] if focus_tag else None

    if mode == "fresh_benchmark":
        n = count or config.DEFAULT_DRILL_SIZE
        rows = conn.execute(
            """SELECT * FROM questions
               WHERE active=1 AND pool='protected_benchmark' AND seen_benchmark=0"""
        ).fetchall()
        rng.shuffle(rows)
        rows = rows[:n]
        return {
            "session_id": "",
            "mode": mode,
            "seed": seed,
            "algo_version": ALGO_VERSION,
            "items": [
                {
                    "question_id": r["id"],
                    "weight": None,
                    "why": [["protected-benchmark-item", 0.0]],
                    "bucket": "protected_unseen",
                }
                for r in rows
            ],
        }

    include_pools = pools_for(mode)
    scored = [
        score_candidate(c, weakness, focus_tags, now) for c in _load_candidates(conn, include_pools)
    ]

    shares = MODE_COMPOSITIONS[mode]
    target = count or sum(v for v in shares.values() if v) or config.DEFAULT_DRILL_SIZE
    chosen_ids = compose(mode, scored, target, weakness, rng, now)
    plan_items = []

    for qid, bucket in chosen_ids.items():
        cand = next((c for c in scored if c.question.id == qid), None)
        if cand is None:
            continue
        plan_items.append(
            {
                "question_id": qid,
                "weight": cand.score,
                "why": cand.components,
                "bucket": bucket,
            }
        )
    rng.shuffle(plan_items)  # spec section 10: randomize presentation order

    # seed reuse or same-second default seeds must never resurrect a session
    sid = session_id(
        mode,
        seed,
        exists=lambda c: (
            conn.execute("SELECT 1 FROM sessions WHERE id=?", (c,)).fetchone() is not None
        ),
    )
    result = {
        "session_id": sid,
        "mode": mode,
        "seed": seed,
        "algo_version": ALGO_VERSION,
        "items": plan_items,
    }
    return result


def persist_session(conn, plan: dict) -> str:
    conn.execute(
        """INSERT OR REPLACE INTO sessions (id, mode, created_at, seed, algo_version, plan_json, status)
           VALUES (?,?,?,?,?,?,'open')""",
        (
            plan["session_id"] or utc_now(),
            plan["mode"],
            utc_now(),
            plan["seed"],
            plan["algo_version"],
            json.dumps(plan["items"]),
        ),
    )
    return plan["session_id"] or utc_now()
