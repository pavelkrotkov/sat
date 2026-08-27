"""How a drill's questions are apportioned between buckets.

Pure: no connection, no clock, no module-level randomness. Everything this
module needs arrives as an argument, which is what makes the interesting
part of drill building testable at all. It used to be three closures inside
a 138-line `select_drill`, reachable only by seeding a database and running
every mode across 25 seeds.

The protected-benchmark guarantee does NOT live here. Protected items are
excluded by a pool filter at query time in `sampler._load_candidates`, so
composition never sees one and cannot reintroduce one however it allocates.
"""

import random

from .. import config
from .candidates import Candidate, row_field
from .spacing import is_due

#: mode -> {bucket: share}. Shares are relative: a drill of a different size
#: scales them proportionally rather than taking the numbers literally.
MODE_COMPOSITIONS: dict[str, dict[str, int | None]] = {
    "targeted_drill": {"old_wrong_due": 4, "old_correct_transfer": 3, "fresh_weak": 5},
    "error_clinic": {"old_wrong_due": 8, "fresh_weak": 2},
    "transfer_drill": {"old_correct_transfer": 6, "fresh_weak": 6},
    "hard_mixed": {"hard_any": 27},
    "fresh_benchmark": {"protected_unseen": None},  # size set by caller
}

#: mode -> pools the sampler may load. The filter that makes protected
#: benchmark leakage structurally impossible outside benchmark mode.
POOLS_BY_MODE: dict[str, tuple[str, ...]] = {
    "targeted_drill": ("historical", "fresh_training"),
    "error_clinic": ("historical",),
    "transfer_drill": ("historical", "fresh_training"),
    "hard_mixed": ("historical", "fresh_training"),
}

FALLBACK_BUCKET = "best_available"


def pools_for(mode: str) -> tuple[str, ...]:
    if mode not in POOLS_BY_MODE:
        raise ValueError(
            f"Invalid mode: {mode}. Must be one of: "
            f"{', '.join(sorted(POOLS_BY_MODE))}, fresh_benchmark"
        )
    return POOLS_BY_MODE[mode]


# ---------------------------------------------------------- bucket rules --

def _is_old_wrong_due(cand: Candidate, weakness: dict) -> bool:
    """Previously missed, and either never drilled in-app or now due."""
    return (
        cand.row["pool"] == "historical"
        and cand.hist_correct == 0
        and (cand.state is None
             or row_field(cand.state, "due_at") is None
             or is_due(cand.state))
    )


def _is_old_correct_transfer(cand: Candidate, weakness: dict) -> bool:
    """Answered correctly before, but on a reasoning tag that is still weak."""
    weak_tags = weakness.get("tag", {})
    return (
        cand.row["pool"] == "historical"
        and cand.hist_correct == 1
        and any(t in weak_tags and weak_tags[t]["score"] >= config.TRANSFER_TAG_THRESHOLD
                for t in cand.tags)
    )


def _is_fresh_weak(cand: Candidate, weakness: dict) -> bool:
    """Unseen, and classified well enough to be aimed at something."""
    return cand.row["pool"] == "fresh_training" and bool(cand.tags or cand.row["official_skill"])


def _is_any(cand: Candidate, weakness: dict) -> bool:
    return True  # weighting already biases hard + weak


BUCKET_RULES = {
    "old_wrong_due": _is_old_wrong_due,
    "old_correct_transfer": _is_old_correct_transfer,
    "fresh_weak": _is_fresh_weak,
    "hard_any": _is_any,
}


def eligible_for(bucket: str, cand: Candidate, weakness: dict) -> bool:
    return BUCKET_RULES.get(bucket, _is_any)(cand, weakness)


# ------------------------------------------------------------ allocation --

def allocate(shares: dict[str, int | None], target: int) -> dict[str, int]:
    """Split `target` across buckets in proportion to `shares`.

    Largest-remainder, so the rounded parts sum to exactly `target` rather
    than to target +/- the number of buckets.
    """
    active = [(bucket, share) for bucket, share in shares.items() if share]
    total_share = sum(share for _, share in active)
    if not active or total_share <= 0:
        return {}
    scale = target / total_share
    scaled = [(bucket, share * scale) for bucket, share in active]
    wants = {bucket: int(value) for bucket, value in scaled}
    leftover = target - sum(wants.values())
    by_remainder = sorted(scaled, key=lambda bv: -(bv[1] - int(bv[1])))
    for bucket, _ in by_remainder[:leftover]:
        wants[bucket] += 1
    return wants


def fallback_allowed(mode: str, cand: Candidate) -> bool:
    """Whether the graceful fill may reach for this candidate.

    Transfer drills contain no memorized errors (spec section 11C) - the
    whole point is questions the student has never got wrong, so the fill
    must not quietly undo that when a bucket runs dry.
    """
    if mode == "transfer_drill" and cand.row["pool"] == "historical" and cand.hist_correct == 0:
        return False
    return True


# -------------------------------------------------------------- compose ---

def compose(mode: str, scored: list[Candidate], target: int, weakness: dict,
            rng: random.Random) -> dict[int, str]:
    """Assign question ids to buckets. Returns {question_id: bucket}."""
    chosen: dict[int, str] = {}

    for bucket, want in allocate(MODE_COMPOSITIONS[mode], target).items():
        pool = [c for c in scored
                if c.row["id"] not in chosen and eligible_for(bucket, c, weakness)]
        pool.sort(key=lambda c: -c.score)
        for cand in pool[:want]:
            chosen[cand.row["id"]] = bucket

    # graceful fill when a bucket ran dry; each mode keeps its own guarantee
    remaining = target - len(chosen)
    if remaining > 0:
        rest = [c for c in scored
                if c.row["id"] not in chosen and fallback_allowed(mode, c)]
        rest.sort(key=lambda c: -c.score)
        rng.shuffle(rest)  # jitter among candidates; slice AFTER shuffling
        for cand in rest[:remaining]:
            chosen[cand.row["id"]] = FALLBACK_BUCKET

    return chosen
