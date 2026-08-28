"""Analytics dashboard aggregation.

transfer_performance is the panel that answers the system's core question:
does training on weak tags transfer to unseen questions? It splits in-app
attempts into four buckets and must never crash on a live corpus.
"""

import pytest

from satprep.analytics import full_dashboard, transfer_performance
from conftest import add_question


def _session(conn, sid, mode):
    conn.execute(
        """INSERT INTO sessions (id, mode, created_at, seed, algo_version, plan_json, status)
           VALUES (?,?,'2026-03-01T00:00:00+00:00','seed','sampler-v1','[]','completed')""",
        (sid, mode),
    )


def _attempt(conn, sid, qid, correct, mode):
    conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                 confidence, time_ms, mode, attempted_at)
           VALUES (?,?,'B',?,2,1000,?,'2026-03-01T00:00:00+00:00')""",
        (sid, qid, int(correct), mode),
    )


def _weak_tag(conn, tag, score=80.0):
    conn.execute(
        """INSERT OR REPLACE INTO weakness_cache (entity_type, entity, score, stats_json, computed_at)
           VALUES ('tag',?,?, '{}', '2026-03-01T00:00:00+00:00')""",
        (tag, score),
    )


@pytest.fixture()
def four_buckets(db):
    """One attempt in each transfer bucket, with a known right/wrong pattern."""
    conn, path = db
    _weak_tag(conn, "qualifier_strength")

    hist = add_question(conn, passage="h", stem="h?", choices=["ha", "hb", "hc", "hd"],
                        source="bluebook_test", pool="historical")
    weak = add_question(conn, passage="w", stem="w?", choices=["wa", "wb", "wc", "wd"],
                        pool="fresh_training", tags=("qualifier_strength",))
    other = add_question(conn, passage="o", stem="o?", choices=["oa", "ob", "oc", "od"],
                         pool="fresh_training", tags=("chronology",))
    prot = add_question(conn, passage="p", stem="p?", choices=["pa", "pb", "pc", "pd"],
                        pool="protected_benchmark")

    _session(conn, "drill1", "targeted_drill")
    _session(conn, "bench1", "fresh_benchmark")
    _attempt(conn, "drill1", hist, correct=1, mode="targeted_drill")
    _attempt(conn, "drill1", weak, correct=0, mode="targeted_drill")
    _attempt(conn, "drill1", other, correct=1, mode="targeted_drill")
    _attempt(conn, "bench1", prot, correct=0, mode="fresh_benchmark")
    conn.commit()
    return conn, {"hist": hist, "weak": weak, "other": other, "prot": prot}


def test_transfer_performance_splits_all_four_buckets(four_buckets):
    conn, _ = four_buckets
    t = transfer_performance(conn)

    assert t["old_exact_questions"] == {"n": 1, "accuracy": 100.0}
    assert t["new_questions_sharing_weak_tags"] == {"n": 1, "accuracy": 0.0}
    assert t["fresh_other"] == {"n": 1, "accuracy": 100.0}
    assert t["protected_benchmark"] == {"n": 1, "accuracy": 0.0}


def test_fresh_training_attempt_does_not_crash(db):
    """Regression: the bucket branch read question_id from a query that
    never selected it, so the dashboard raised IndexError as soon as any
    in-app attempt landed on a fresh_training question."""
    conn, _ = db
    qid = add_question(conn, passage="f", stem="f?", choices=["a", "b", "c", "d"],
                       pool="fresh_training", tags=("qualifier_strength",))
    _session(conn, "s1", "targeted_drill")
    _attempt(conn, "s1", qid, correct=1, mode="targeted_drill")
    conn.commit()

    t = transfer_performance(conn)
    assert t["fresh_other"]["n"] + t["new_questions_sharing_weak_tags"]["n"] == 1


def test_weak_tag_threshold_discriminates_buckets(db):
    """A fresh question only counts as transfer material when one of its
    tags is actually weak (score >= 55) - otherwise it is fresh_other."""
    conn, _ = db
    _weak_tag(conn, "qualifier_strength", score=80.0)
    _weak_tag(conn, "chronology", score=20.0)

    strong = add_question(conn, passage="s", stem="s?", choices=["sa", "sb", "sc", "sd"],
                          pool="fresh_training", tags=("chronology",))
    weak = add_question(conn, passage="w", stem="w?", choices=["wa", "wb", "wc", "wd"],
                        pool="fresh_training", tags=("qualifier_strength",))
    _session(conn, "s1", "targeted_drill")
    _attempt(conn, "s1", strong, correct=1, mode="targeted_drill")
    _attempt(conn, "s1", weak, correct=1, mode="targeted_drill")
    conn.commit()

    t = transfer_performance(conn)
    assert t["new_questions_sharing_weak_tags"]["n"] == 1
    assert t["fresh_other"]["n"] == 1


def test_historical_attempts_are_excluded(db):
    """Transfer is about in-app practice; the scraped history is the baseline."""
    conn, _ = db
    qid = add_question(conn, passage="h", stem="h?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical")
    _session(conn, "hist:x", "historical")
    _attempt(conn, "hist:x", qid, correct=0, mode="historical")
    conn.commit()

    t = transfer_performance(conn)
    assert all(bucket["n"] == 0 for bucket in t.values())


def test_benchmark_bucket_survives_pool_flip(db):
    """mark_benchmark_seen moves an answered item into fresh_training; the
    attempt must stay in the benchmark bucket, keyed on the session mode."""
    from satprep.corpus.ingest import mark_benchmark_seen

    conn, _ = db
    qid = add_question(conn, passage="p", stem="p?", choices=["a", "b", "c", "d"],
                       pool="protected_benchmark")
    _session(conn, "bench1", "fresh_benchmark")
    _attempt(conn, "bench1", qid, correct=1, mode="fresh_benchmark")
    mark_benchmark_seen(conn, [qid])
    conn.commit()

    t = transfer_performance(conn)
    assert t["protected_benchmark"] == {"n": 1, "accuracy": 100.0}
    assert t["fresh_other"]["n"] == 0


def test_full_dashboard_renders_with_in_app_attempts(four_buckets):
    """The crash was reachable from GET / and `satprep stats`, both of which
    go through full_dashboard."""
    conn, _ = four_buckets

    d = full_dashboard(conn)
    assert set(d) == {"corpus", "skills", "tags", "misconceptions", "transfer",
                      "recent_trend", "next_action", "recent_sessions"}
    # every in-app attempt is accounted for in exactly one bucket
    assert sum(b["n"] for b in d["transfer"].values()) == 4


# ------------------------------------------------- one owner of the score --

def test_risk_score_comes_from_the_weakness_model(db):
    """Regression: analytics used `model.get(x) or _smoothed_rate(...)`, so a
    cache miss silently produced a differently-computed number in the same
    column. The model is now the only source."""
    from satprep.analytics import skill_accuracy, tag_accuracy
    from satprep.training.weakness import compute_weakness

    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical",
                       skill="Inferences", tags=("qualifier_strength",))
    _session(conn, "hist:x", "historical")
    _attempt(conn, "hist:x", qid, correct=0, mode="historical")
    conn.commit()

    model = compute_weakness(conn)
    skill = next(s for s in skill_accuracy(conn) if s["skill"] == "Inferences")
    tag = next(t for t in tag_accuracy(conn) if t["tag"] == "qualifier_strength")

    assert skill["risk_score"] == model["skill"]["Inferences"]["score"]
    assert tag["risk_score"] == model["tag"]["qualifier_strength"]["score"]


def test_empty_cache_is_computed_not_papered_over(db):
    """With no weakness_cache row, the old code fell back to its own prior.
    risk_scores computes instead."""
    from satprep.analytics import skill_accuracy

    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical", skill="Inferences")
    _session(conn, "hist:x", "historical")
    _attempt(conn, "hist:x", qid, correct=0, mode="historical")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM weakness_cache").fetchone()[0] == 0

    rows = skill_accuracy(conn)

    assert rows[0]["risk_score"] > 0
    assert conn.execute("SELECT COUNT(*) FROM weakness_cache").fetchone()[0] > 0


def test_risk_scores_recomputes_at_most_once(db, monkeypatch):
    """A skill with questions but no attempts is legitimately absent from the
    model; asking for it must not send risk_scores into a recompute loop."""
    import satprep.training.weakness as weakness_mod

    conn, _ = db
    add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"],
                 source="bluebook_test", pool="historical", skill="Inferences")
    conn.commit()

    calls = []
    real = weakness_mod.compute_weakness
    monkeypatch.setattr(weakness_mod, "compute_weakness",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    assert weakness_mod.risk_scores(conn, "skill", ["Inferences"]) == {}
    assert len(calls) == 1


def test_weak_tag_threshold_is_configurable(db, monkeypatch):
    """The transfer panel's 55 was a magic number inline in a SQL string."""
    from satprep import config
    from satprep.analytics import transfer_performance

    conn, _ = db
    _weak_tag(conn, "qualifier_strength", score=60.0)
    qid = add_question(conn, passage="w", stem="w?", choices=["wa", "wb", "wc", "wd"],
                       pool="fresh_training", tags=("qualifier_strength",))
    _session(conn, "s1", "targeted_drill")
    _attempt(conn, "s1", qid, correct=1, mode="targeted_drill")
    conn.commit()

    assert transfer_performance(conn)["new_questions_sharing_weak_tags"]["n"] == 1

    monkeypatch.setattr(config, "WEAK_TAG_THRESHOLD", 90.0)
    assert transfer_performance(conn)["new_questions_sharing_weak_tags"]["n"] == 0
    assert transfer_performance(conn)["fresh_other"]["n"] == 1


def test_cached_profile_is_ranked_and_typed(db):
    """The weakness screen and the drill picker read the profile here rather
    than querying weakness_cache, so ordering is part of the interface."""
    from satprep.training.weakness import cached_profile

    conn, _ = db
    for tag, score in (("chronology", 20.0), ("qualifier_strength", 80.0),
                       ("scope_shift", 50.0)):
        _weak_tag(conn, tag, score=score)
    conn.commit()

    profile = cached_profile(conn, "tag")
    assert list(profile) == ["qualifier_strength", "scope_shift", "chronology"]
    assert profile["qualifier_strength"]["score"] == 80.0

    both = cached_profile(conn)
    assert set(both) == {"tag"}


def test_dashboard_sections_share_one_model_snapshot(db):
    """Regression: risk_scores refreshes lazily on a cache miss. If the miss
    happened while assembling the tags section, compute_weakness rewrote the
    skill scores the skills section had already read, and one response showed
    two model snapshots. full_dashboard settles the cache up front."""
    import satprep.analytics as analytics_mod
    from satprep.training.weakness import compute_weakness

    from satprep.db import connect

    conn, path = db
    old = add_question(conn, passage="o", stem="o?", choices=["oa", "ob", "oc", "od"],
                       source="bluebook_test", pool="historical", skill="Inferences")
    _session(conn, "hist:a", "historical")
    _attempt(conn, "hist:a", old, correct=0, mode="historical")
    conn.commit()
    compute_weakness(conn)  # cache now covers Inferences, no tags

    # ingest arrives: more evidence for the same skill, plus a brand-new tag
    fresh = add_question(conn, passage="n", stem="n?", choices=["na", "nb", "nc", "nd"],
                         source="bluebook_test", pool="historical",
                         skill="Inferences", tags=("qualifier_strength",))
    _session(conn, "hist:b", "historical")
    _attempt(conn, "hist:b", fresh, correct=0, mode="historical")
    conn.commit()

    d = full_dashboard(conn)

    dashboard_skill = next(s for s in d["skills"] if s["skill"] == "Inferences")
    persisted = conn.execute(
        "SELECT score FROM weakness_cache WHERE entity_type='skill' AND entity='Inferences'"
    ).fetchone()["score"]
    assert dashboard_skill["risk_score"] == persisted
    assert any(t["tag"] == "qualifier_strength" for t in d["tags"])


def test_ensure_current_is_a_no_op_when_cache_covers_evidence(db, monkeypatch):
    """It runs on every dashboard load, so it must not recompute needlessly."""
    import satprep.training.weakness as weakness_mod

    conn, _ = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical",
                       skill="Inferences", tags=("qualifier_strength",))
    _session(conn, "hist:x", "historical")
    _attempt(conn, "hist:x", qid, correct=0, mode="historical")
    conn.commit()
    weakness_mod.compute_weakness(conn)

    calls = []
    monkeypatch.setattr(weakness_mod, "compute_weakness",
                        lambda *a, **k: calls.append(1))
    weakness_mod.ensure_current(conn)

    assert calls == []
