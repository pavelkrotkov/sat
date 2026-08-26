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
    from satprep.ingest import mark_benchmark_seen

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


def test_full_dashboard_renders_with_in_app_attempts(four_buckets, monkeypatch):
    """The crash was reachable from GET / and `satprep stats`, both of which
    go through full_dashboard."""
    conn, _ = four_buckets
    monkeypatch.setattr("satprep.analytics.connect", lambda db_path=None: conn)

    d = full_dashboard()
    assert set(d) == {"corpus", "skills", "tags", "misconceptions", "transfer", "recent_trend"}
    assert d["transfer"]["new_questions_sharing_weak_tags"]["n"] == 1
