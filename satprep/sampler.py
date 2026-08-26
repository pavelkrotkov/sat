"""Explainable weighted question sampler (spec sections 9, 10, 11, 18, 21).

Every candidate receives an additive score whose components are stored in
the session plan so the admin UI can answer: why was this question selected?

Protected benchmark questions are excluded by FILTERING at the query level
in every non-benchmark mode; they are never reachable through weights.
"""

import json
import random
from datetime import datetime

from . import ALGO_VERSION, config
from .ingest import utc_now
from .spacing import is_due
from .tags import tags_by_question


def row_field(state, key: str, default=None):
    """Read a field from sqlite3.Row or a duck-typed state object."""
    if state is None:
        return default
    try:
        return state[key]
    except (KeyError, TypeError, IndexError):
        return getattr(state, key, default)


class Candidate:
    __slots__ = ("row", "tags", "components", "score", "state", "hist_correct")

    def __init__(self, row, tags):
        self.row = row
        self.tags = tags
        self.components: list[tuple[str, float]] = []
        self.score = 0.0
        self.state = None
        self.hist_correct = None  # True/False from scraped history, else None

    def add(self, label: str, value: float) -> None:
        if abs(value) > 1e-9:
            self.components.append((label, round(value, 2)))
            self.score += value


MODE_COMPOSITIONS = {
    # mode -> {bucket: count}; buckets cascade gracefully when pools run dry
    "targeted_drill": {"old_wrong_due": 4, "old_correct_transfer": 3, "fresh_weak": 5},
    "error_clinic": {"old_wrong_due": 8, "fresh_weak": 2},
    "transfer_drill": {"old_correct_transfer": 6, "fresh_weak": 6},
    "hard_mixed": {"hard_any": 27},
    "fresh_benchmark": {"protected_unseen": None},  # size set by caller
}


def _load_candidates(conn, include_pools: tuple[str, ...]) -> list[Candidate]:
    qmarks = ",".join("?" for _ in include_pools)
    rows = conn.execute(
        f"""SELECT * FROM questions
            WHERE active=1 AND pool IN ({qmarks}) AND choices_json != '[]'""",
        include_pools,
    ).fetchall()
    tag_map = tags_by_question(conn)
    state_map = {r["question_id"]: r for r in conn.execute("SELECT * FROM question_state")}
    hist_map: dict[int, int] = {}
    for r in conn.execute("SELECT question_id, MAX(correct) AS c FROM attempts WHERE mode='historical' GROUP BY question_id"):
        hist_map[r["question_id"]] = r["c"]
    out = []
    for row in rows:
        c = Candidate(row, tag_map.get(row["id"], []))
        c.state = state_map.get(row["id"])
        c.hist_correct = hist_map.get(row["id"])
        out.append(c)
    return out


def score_candidate(cand: Candidate, weakness: dict, focus_tags: list[str] | None = None,
                    now: datetime | None = None) -> Candidate:
    """Additive explainable score (mirrors config weights)."""
    now = now or datetime.now().astimezone()
    q = cand.row
    tags = cand.tags
    weak_tags = weakness.get("tag", {})
    weak_skills = weakness.get("skill", {})

    cand.add("base", 0.5)

    matched = [t for t in tags if t in weak_tags]
    matched.sort(key=lambda t: -weak_tags[t]["score"])
    if matched:
        top = matched[0]
        w = weak_tags[top]["score"] / 100.0 * 10.0
        bonus = config.W_WEAK_TAG_MATCH * w / 10.0
        if focus_tags and top in focus_tags:
            bonus *= 1.35
        cand.add(f"weak-tag:{top}", bonus)
        if len(matched) > 1:
            second = matched[1]
            cand.add(
                f"weak-tag-2nd:{second}",
                config.W_WEAK_SECONDARY_TAG * (weak_tags[second]["score"] / 100.0),
            )
    skill = q["official_skill"]
    if skill and skill in weak_skills:
        cand.add(f"skill-weakness:{skill}", config.W_SKILL_WEAKNESS * weak_skills[skill]["score"] / 100.0)
    if skill in config.SEMANTIC_SKILLS:
        # spec section 16: default bias toward hard semantic/reasoning content
        cand.add("semantic-content-bias", config.W_SEMANTIC_BIAS)

    diff_bonus = {"hard": config.W_HARD_DIFFICULTY, "medium": 0.5}.get(q["difficulty"], 0.7)
    cand.add("difficulty" + (f":{q['difficulty']}" if q["difficulty"] else ":unknown"), diff_bonus)

    state = cand.state
    def _sget(key, default=0):
        return row_field(state, key, default)

    seen_times = _sget("times_seen")
    hist_correct = cand.hist_correct if q["pool"] == "historical" else False
    # an item scraped as incorrect counts as due even before any in-app review
    due_now = is_due(state) or (q["pool"] == "historical" and hist_correct == 0)
    if q["pool"] == "fresh_training":
        if matched:
            cand.add("fresh-matching-weak-tags", config.W_FRESH_MATCHING_WEAK)
        elif skill and skill in weak_skills:
            cand.add("fresh-neighbor-skill", config.W_FRESH_NEIGHBOR)
    if q["pool"] == "historical" and due_now and hist_correct == 0:
        cand.add("due-for-review-previously-wrong", config.W_DUE_INCORRECT)
    if q["pool"] == "historical" and hist_correct == 1 and matched:
        cand.add("transfer-correct-shares-weak-tag", config.W_TRANSFER_CORRECT)

    exposure_penalty = min(config.PENALTY_EXPOSURE_CAP, config.PENALTY_EXPOSURE_PER_SEEN * seen_times)
    if exposure_penalty:
        cand.add("exposure-penalty", -exposure_penalty)
    last_attempted = _sget("last_attempted_at", None)
    if last_attempted:
        try:
            days_since = (now - datetime.fromisoformat(last_attempted)).days
            if days_since >= 45:
                cand.add(f"not-seen-in-{min(days_since,999)}-days", config.W_NOT_SEEN_LONG_AGO)
        except ValueError:
            pass
    times_correct = _sget("times_correct")
    streak = _sget("confident_wrong_streak")
    if times_correct >= 2 and streak == 0:
        cand.add("recently-mastered-penalty", -config.PENALTY_RECENT_MASTERED)
    return cand


def select_drill(conn, mode: str, count: int | None = None, seed: str | None = None,
                 focus_tag: str | None = None) -> dict:
    """Build a drill plan. Returns {'session_id', 'items': [...], 'seed'}.

    items: [{'question_id', 'weight', 'why': [(component, delta)], 'bucket'}]
    Protected benchmark leakage is structurally impossible outside
    fresh_benchmark mode because candidates are filtered by pool.
    """
    seed = seed or utc_now()
    rng = random.Random(seed)
    now = datetime.now().astimezone()

    from .weakness import cached_profile, compute_weakness
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
            "session_id": "", "mode": mode, "seed": seed, "algo_version": ALGO_VERSION,
            "items": [
                {"question_id": r["id"], "weight": None, "why": [["protected-benchmark-item", 0.0]], "bucket": "protected_unseen"}
                for r in rows
            ],
        }

    pools_by_mode = {
        "targeted_drill": ("historical", "fresh_training"),
        "error_clinic": ("historical",),
        "transfer_drill": ("historical", "fresh_training"),
        "hard_mixed": ("historical", "fresh_training"),
    }
    if mode not in pools_by_mode:
        raise ValueError(
            f"Invalid mode: {mode}. Must be one of: {', '.join(sorted(pools_by_mode))}, fresh_benchmark"
        )
    include_pools = pools_by_mode[mode]

    candidates = _load_candidates(conn, include_pools)
    scored = [score_candidate(c, weakness, focus_tags, now) for c in candidates]

    comp = MODE_COMPOSITIONS[mode]
    total_target = count or sum(v for v in comp.values() if v) or config.DEFAULT_DRILL_SIZE
    scale = total_target / max(1, sum(v for v in comp.values() if v))
    # largest-remainder allocation so rounded shares sum EXACTLY to target

    chosen_ids: dict[int, str] = {}
    plan_items = []

    def bucket_pool(name: str) -> list[Candidate]:
        if name == "old_wrong_due":
            # due = never scheduled / never drilled in-app yet, or schedule says due
            pred = lambda c: (  # noqa: E731
                c.row["pool"] == "historical"
                and c.hist_correct == 0
                and (c.state is None
                     or row_field(c.state, "due_at") is None
                     or is_due(c.state))
            )
        elif name == "old_correct_transfer":
            pred = lambda c: (  # noqa: E731
                c.row["pool"] == "historical"
                and c.hist_correct == 1  # previously answered CORRECTLY only
                and any(t in weakness.get("tag", {}) and weakness["tag"][t]["score"] >= 45 for t in c.tags)
            )
        elif name == "fresh_weak":
            pred = lambda c: c.row["pool"] == "fresh_training" and (c.tags or c.row["official_skill"])  # noqa: E731
        elif name == "hard_any":
            pred = lambda c: True  # weighting already biases hard+weak  # noqa: E731
        else:
            pred = lambda c: True  # noqa: E731
        return [c for c in scored if c.row["id"] not in chosen_ids and pred(c)]

    active = [(b, (share or 0) * scale) for b, share in comp.items() if share]
    wants = {b: int(x) for b, x in active}
    leftover = total_target - sum(wants.values())
    fracs = sorted(active, key=lambda bx: -(bx[1] - int(bx[1])))
    for b, _x in fracs[:leftover]:
        wants[b] += 1
    for bucket, want in wants.items():
        pool_cands = bucket_pool(bucket)
        pool_cands.sort(key=lambda c: -c.score)
        for c in pool_cands[:want]:
            chosen_ids[c.row["id"]] = bucket

    # graceful fill if composition underfilled; each mode keeps its guarantee
    remaining = total_target - len(chosen_ids)
    if remaining > 0:
        def _eligible_fallback(c):
            if c.row["id"] in chosen_ids:
                return False
            if mode == "transfer_drill" and c.row["pool"] == "historical" and c.hist_correct == 0:
                return False  # spec section 11C: transfer drills contain no memorized errors
            return True
        rest = [c for c in scored if _eligible_fallback(c)]
        rest.sort(key=lambda c: -c.score)
        rng.shuffle(rest)  # jitter among candidates; slice AFTER shuffling
        for c in rest[:max(0, remaining)]:
            chosen_ids[c.row["id"]] = "best_available"

    for qid, bucket in chosen_ids.items():
        cand = next((c for c in scored if c.row["id"] == qid), None)
        if cand is None:
            continue
        plan_items.append({
            "question_id": qid,
            "weight": cand.score,
            "why": cand.components,
            "bucket": bucket,
        })
    rng.shuffle(plan_items)  # spec section 10: randomize presentation order

    import uuid
    session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mode}:{seed}").hex[:16]
    while conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
        # seed reuse or same-second default seeds must never resurrect a session
        session_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{mode}:{seed}:{uuid.uuid4()}").hex[:16]
    result = {
        "session_id": session_id,
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
            plan["session_id"] or utc_now(), plan["mode"], utc_now(), plan["seed"],
            plan["algo_version"], json.dumps(plan["items"]),
        ),
    )
    return plan["session_id"] or utc_now()
