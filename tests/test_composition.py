"""Drill composition, with no database in sight.

These rules - which candidates a bucket accepts, how a target is split
between buckets, what the graceful fill may reach for - used to be closures
inside a 138-line `select_drill`. The only way to exercise them was to seed
a database and run every mode across 25 seeds, which is why
tests/test_pools_leakage.py looks the way it does. That test stays as the
end-to-end safety net; this one pins the behaviour directly.
"""

import random

import pytest

from satprep import config
from satprep.training.candidates import Candidate
from satprep.training.composition import (
    FALLBACK_BUCKET, MODE_COMPOSITIONS, allocate, compose, eligible_for,
    fallback_allowed, pools_for,
)

WEAKNESS = {"tag": {"qualifier_strength": {"score": 80.0},
                    "chronology": {"score": 10.0}}}


def cand(qid, *, pool="fresh_training", hist_correct=None, tags=("qualifier_strength",),
         skill="Inferences", score=1.0, state=None):
    c = Candidate({"id": qid, "pool": pool, "official_skill": skill}, list(tags))
    c.hist_correct = hist_correct
    c.state = state
    c.score = score
    return c


def rng():
    return random.Random("fixed")


# ------------------------------------------------------------ allocation --

@pytest.mark.parametrize("mode", sorted(m for m in MODE_COMPOSITIONS if m != "fresh_benchmark"))
@pytest.mark.parametrize("target", range(1, 31))
def test_allocation_sums_exactly_to_target(mode, target):
    """Largest-remainder: the rounded shares must total the target, not
    target +/- the number of buckets."""
    wants = allocate(MODE_COMPOSITIONS[mode], target)
    assert sum(wants.values()) == target
    assert all(n >= 0 for n in wants.values())


def test_allocation_is_proportional():
    wants = allocate({"a": 4, "b": 3, "c": 5}, 12)
    assert wants == {"a": 4, "b": 3, "c": 5}
    assert allocate({"a": 4, "b": 3, "c": 5}, 24) == {"a": 8, "b": 6, "c": 10}


def test_allocation_handles_degenerate_shares():
    assert allocate({}, 10) == {}
    assert allocate({"a": None}, 10) == {}
    assert allocate({"a": 0}, 10) == {}


# ---------------------------------------------------------- bucket rules --

def test_old_wrong_due_needs_a_previous_miss():
    missed = cand(1, pool="historical", hist_correct=0)
    got_right = cand(2, pool="historical", hist_correct=1)
    fresh = cand(3, pool="fresh_training")

    assert eligible_for("old_wrong_due", missed, WEAKNESS)
    assert not eligible_for("old_wrong_due", got_right, WEAKNESS)
    assert not eligible_for("old_wrong_due", fresh, WEAKNESS)


def test_old_correct_transfer_needs_a_still_weak_tag():
    """A question answered correctly is only worth re-testing if the reasoning
    it exercises is still shaky."""
    weak = cand(1, pool="historical", hist_correct=1, tags=("qualifier_strength",))
    strong = cand(2, pool="historical", hist_correct=1, tags=("chronology",))
    never_seen = cand(3, pool="historical", hist_correct=0, tags=("qualifier_strength",))

    assert eligible_for("old_correct_transfer", weak, WEAKNESS)
    assert not eligible_for("old_correct_transfer", strong, WEAKNESS)
    assert not eligible_for("old_correct_transfer", never_seen, WEAKNESS)


def test_transfer_threshold_is_configurable(monkeypatch):
    borderline = cand(1, pool="historical", hist_correct=1, tags=("chronology",))
    assert not eligible_for("old_correct_transfer", borderline, WEAKNESS)

    monkeypatch.setattr(config, "TRANSFER_TAG_THRESHOLD", 5.0)
    assert eligible_for("old_correct_transfer", borderline, WEAKNESS)


def test_fresh_weak_needs_some_classification():
    """An unclassified fresh question cannot be aimed at anything."""
    tagged = cand(1, tags=("qualifier_strength",), skill="")
    skilled = cand(2, tags=(), skill="Inferences")
    blank = cand(3, tags=(), skill="")

    assert eligible_for("fresh_weak", tagged, WEAKNESS)
    assert eligible_for("fresh_weak", skilled, WEAKNESS)
    assert not eligible_for("fresh_weak", blank, WEAKNESS)


# ------------------------------------------------------------- compose ----

def test_compose_fills_buckets_by_score():
    """Within a bucket, the highest-scoring eligible candidates win.

    error_clinic splits a target of 4 into 3 old_wrong_due and 1 fresh_weak.
    With no fresh candidates to offer, the fresh_weak slot cascades to the
    fallback - which shuffles, so only the bucketed picks are deterministic.
    """
    scored = [cand(i, pool="historical", hist_correct=0, score=float(i)) for i in range(1, 11)]
    chosen = compose("error_clinic", scored, 4, WEAKNESS, rng())

    bucketed = {qid for qid, bucket in chosen.items() if bucket == "old_wrong_due"}
    assert bucketed == {10, 9, 8}
    assert len(chosen) == 4
    assert list(chosen.values()).count(FALLBACK_BUCKET) == 1


def test_underfilled_buckets_cascade_without_overshooting():
    """error_clinic wants 8 old_wrong_due and 2 fresh_weak. Given only two of
    the former, the rest must come from the fallback and the total must still
    land on the target."""
    scored = [cand(1, pool="historical", hist_correct=0),
              cand(2, pool="historical", hist_correct=0)]
    scored += [cand(i, pool="fresh_training") for i in range(3, 15)]

    chosen = compose("error_clinic", scored, 10, WEAKNESS, rng())

    assert len(chosen) == 10
    assert chosen[1] == "old_wrong_due" and chosen[2] == "old_wrong_due"
    assert FALLBACK_BUCKET in chosen.values()


def test_compose_never_exceeds_target_or_repeats():
    scored = [cand(i, pool="historical", hist_correct=0) for i in range(1, 40)]
    for target in (1, 5, 12, 27):
        chosen = compose("targeted_drill", scored, target, WEAKNESS, rng())
        assert len(chosen) == target
        assert len(set(chosen)) == target


def test_compose_returns_everything_available_when_short():
    scored = [cand(1, pool="historical", hist_correct=0), cand(2, pool="fresh_training")]
    chosen = compose("targeted_drill", scored, 12, WEAKNESS, rng())
    assert set(chosen) == {1, 2}


# --------------------------------------------------- mode-specific rules --

def test_transfer_drill_never_serves_a_memorized_error():
    """Spec section 11C. The fallback fill is the path that used to threaten
    this: when a bucket runs dry it reaches across the whole scored set."""
    memorized = [cand(i, pool="historical", hist_correct=0) for i in range(1, 20)]
    transferable = [cand(100, pool="historical", hist_correct=1)]

    chosen = compose("transfer_drill", memorized + transferable, 12, WEAKNESS, rng())

    assert set(chosen).isdisjoint({c.row["id"] for c in memorized})
    assert set(chosen) == {100}   # only the transferable item survives


def test_fallback_allowed_is_mode_specific():
    memorized = cand(1, pool="historical", hist_correct=0)
    assert not fallback_allowed("transfer_drill", memorized)
    assert fallback_allowed("error_clinic", memorized)
    assert fallback_allowed("targeted_drill", memorized)


def test_error_clinic_honours_its_share_when_the_pool_is_deep():
    scored = [cand(i, pool="historical", hist_correct=0) for i in range(1, 30)]
    scored += [cand(i, pool="fresh_training") for i in range(100, 130)]

    chosen = compose("error_clinic", scored, 10, WEAKNESS, rng())

    buckets = [chosen[q] for q in chosen]
    assert buckets.count("old_wrong_due") == 8
    assert buckets.count("fresh_weak") == 2


def test_pools_for_rejects_unknown_modes():
    assert pools_for("error_clinic") == ("historical",)
    with pytest.raises(ValueError, match="Invalid mode"):
        pools_for("nonsense")


def test_protected_benchmark_is_not_composition_s_job():
    """The guarantee is a pool filter at query time, not a scoring or bucket
    effect - composition never sees a protected item, so it cannot leak one.
    No mode's pools include the protected pool."""
    for mode in MODE_COMPOSITIONS:
        if mode == "fresh_benchmark":
            continue
        assert "protected_benchmark" not in pools_for(mode)
