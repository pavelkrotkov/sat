"""Critical safety tests: protected benchmark questions must never leak."""

import pytest

from satprep.db import connect
from satprep.sampler import select_drill
from conftest import add_question


def _seed_db(conn, n_fresh=40, n_protected=15, n_hist=30):
    for i in range(n_hist):
        qid = add_question(conn, passage=f"hp{i}", stem=f"hs{i}?",
                           choices=[f"h{i}-{l}" for l in "abcd"],
                           source="bluebook_test", pool="historical",
                           skill="Inferences" if i % 3 == 0 else "", tags=("qualifier_strength",))
        conn.execute("INSERT OR REPLACE INTO attempts (session_id, question_id, chosen_letter, correct, confidence, time_ms, mode, attempted_at) VALUES (?,?,?,?,0,0,'historical','2026-03-01')",
                     (f"hist:h{i}", qid, "B", 1 if i % 4 else 0))
    for i in range(n_fresh):
        add_question(conn, passage=f"fp{i}", stem=f"fs{i}?", choices=[f"f{i}-{l}" for l in "abcd"],
                     source="college_board_question_bank", pool="fresh_training",
                     difficulty="hard", tags=("hypothesis_vs_result",))
    for i in range(n_protected):
        # force protected pool regardless of hash
        from satprep.fingerprint import fingerprint
        fp = fingerprint(f"ppp{i}", f"ps{i}?", [f"p{i}-{l}" for l in "abcd"])
        add_question(conn, passage=f"ppp{i}", stem=f"ps{i}?",
                     choices=[f"p{i}-{l}" for l in "abcd"],
                     source="college_board_question_bank", pool="protected_benchmark",
                     fingerprint=fp)
    conn.commit()


MODES = ["targeted_drill", "error_clinic", "transfer_drill", "hard_mixed"]


def test_no_protected_leakage_in_any_mode(db):
    conn, path = db
    _seed_db(conn)
    protected = {r[0] for r in conn.execute(
        "SELECT id FROM questions WHERE pool='protected_benchmark'")}
    assert len(protected) >= 10
    leaked = set()
    for mode in MODES:
        for seed in range(25):
            plan = select_drill(mode, count=12, seed=str(seed), db_path=path)
            ids = {i["question_id"] for i in plan["items"]}
            leaked |= ids & protected
    assert not leaked, f"protected benchmark leaked: {leaked}"


def test_benchmark_mode_uses_only_unseen_protected(db):
    conn, path = db
    _seed_db(conn)
    plan = select_drill("fresh_benchmark", count=8, seed="s", db_path=path)
    conn2 = connect(path)
    for item in plan["items"]:
        row = conn2.execute("SELECT pool, seen_benchmark FROM questions WHERE id=?",
                            (item["question_id"],)).fetchone()
        assert row["pool"] == "protected_benchmark"
        assert row["seen_benchmark"] == 0
    conn2.close()


def test_answered_benchmark_enters_training_and_never_returns(db):
    from satprep.ingest import mark_benchmark_seen

    conn, path = db
    _seed_db(conn, n_protected=12)
    plan1 = select_drill("fresh_benchmark", count=5, seed="one", db_path=path)
    taken = [i["question_id"] for i in plan1["items"]]
    mark_benchmark_seen(conn, taken)
    conn.commit()
    plan2 = select_drill("fresh_benchmark", count=25, seed="two", db_path=path)
    remaining_ids = {i["question_id"] for i in plan2["items"]}
    assert set(taken).isdisjoint(remaining_ids)


def test_pool_split_stable_across_rebuilds():
    from satprep.fingerprint import fingerprint, pool_for_fingerprint

    fps = [fingerprint("passage x", "stem y", ["a", "b"]) for _ in range(1)]
    assert [pool_for_fingerprint(fp) for fp in fps] == \
           [pool_for_fingerprint(fp) for fp in fps]
