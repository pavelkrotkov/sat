from satprep.training.sampler import select_drill
from conftest import add_question


def _seed(db, n_hist_wrong=6, n_hist_right=10, n_fresh=14):
    conn, path = db
    for i in range(n_hist_wrong):
        qid = add_question(conn, passage=f"w{i}", stem=f"ws{i}?",
                           choices=[f"w{i}c{l}" for l in "abcd"],
                           source="bluebook_test", pool="historical",
                           tags=("qualifier_strength",))
        conn.execute("INSERT INTO attempts (session_id, question_id, chosen_letter, correct, confidence, time_ms, mode, attempted_at) VALUES (?,?,?,?,0,0,'historical','2026-03-01')",
                     (f"hw{i}", qid, "B", 0))
    for i in range(n_hist_right):
        qid = add_question(conn, passage=f"r{i}", stem=f"rs{i}?",
                           choices=[f"r{i}c{l}" for l in "abcd"],
                           source="bluebook_test", pool="historical",
                           tags=("qualifier_strength", "paraphrase_precision"))
        conn.execute("INSERT INTO attempts (session_id, question_id, chosen_letter, correct, confidence, time_ms, mode, attempted_at) VALUES (?,?,?,1,2,0,'historical','2026-03-01')",
                     (f"hr{i}", qid, "A"))
    for i in range(n_fresh):
        add_question(conn, passage=f"f{i}", stem=f"fs{i}?",
                     choices=[f"f{i}c{l}" for l in "abcd"],
                     source="college_board_question_bank", pool="fresh_training",
                     difficulty="hard", tags=("qualifier_strength",))
    conn.commit()
    return conn


def test_deterministic_for_same_seed(db):
    conn, _ = db
    conn = _seed(db)
    a = select_drill(conn, "targeted_drill", count=12, seed="fixed")
    b = select_drill(conn, "targeted_drill", count=12, seed="fixed")
    assert [i["question_id"] for i in a["items"]] == [i["question_id"] for i in b["items"]]


def test_plan_has_explainable_components(db):
    conn = _seed(db)
    plan = select_drill(conn, "targeted_drill", count=6, seed="x")
    assert plan["items"]
    for item in plan["items"]:
        assert item["why"], "every selection must record its reasons"
        labels = {lbl for lbl, _ in item["why"]}
        assert any(l.startswith(("weak-tag", "skill-weakness", "difficulty", "base")) for l in labels)


def test_targeted_mix_includes_old_wrong_and_fresh(db):
    conn = _seed(db)
    plan = select_drill(conn, "targeted_drill", count=12, seed="mix")
    buckets = {i["bucket"] for i in plan["items"]}
    assert "old_wrong_due" in buckets
    assert "fresh_weak" in buckets
    assert len(plan["items"]) == 12
    ids = [i["question_id"] for i in plan["items"]]
    assert len(ids) == len(set(ids)), "no duplicate questions within a drill"


def test_transfer_mode_excludes_previously_wrong(db):
    conn = _seed(db)
    wrong_ids = {r[0] for r in conn.execute(
        """SELECT question_id FROM attempts WHERE correct=0 AND mode='historical'""")}
    plan = select_drill(conn, "transfer_drill", count=10, seed="t")
    ids = {i["question_id"] for i in plan["items"]}
    assert not ids & wrong_ids, "transfer drill must not serve memorized errors"


def test_error_clinic_prefers_due_errors(db):
    conn = _seed(db)
    plan = select_drill(conn, "error_clinic", count=8, seed="e")
    buckets = {i["bucket"] for i in plan["items"]}
    assert "old_wrong_due" in buckets


def test_exposure_penalty_demotes_seen_questions(db):
    from satprep.training.sampler import Candidate, score_candidate

    conn = _seed(db)
    c = conn
    row = c.execute("""SELECT q.* FROM questions q WHERE q.pool='historical' LIMIT 1""").fetchone()
    tags = ["qualifier_strength"]
    weakness = {"tag": {"qualifier_strength": {"score": 60}},
                "skill": {"Inferences": {"score": 40}}}
    from types import SimpleNamespace
    fresh = Candidate(row, tags)
    fresh.state = SimpleNamespace(times_seen=0, times_correct=0, times_wrong=0,
                                  confident_wrong_streak=0, due_at=None, last_attempted_at=None,
                                  interval_days=1.0)
    seen = Candidate(row, tags)
    seen.state = SimpleNamespace(times_seen=4, times_correct=0, times_wrong=4,
                                 confident_wrong_streak=1, due_at=None, last_attempted_at=None,
                                 interval_days=1.0)
    score_candidate(fresh, weakness)
    score_candidate(seen, weakness)
    assert fresh.score > seen.score
    c.close()


def test_bucket_allocation_never_overshoots(db):
    conn = _seed(db)
    for n in (11, 13, 14, 27):
        plan = select_drill(conn, "targeted_drill", count=n, seed=f"cap{n}")
        assert len(plan["items"]) == n, f"count={n} returned {len(plan['items'])}"


def test_seed_reuse_creates_distinct_sessions(db):
    from satprep.training.sampler import persist_session

    conn = _seed(db)
    p1 = select_drill(conn, "hard_mixed", count=6, seed="same")
    sid1 = persist_session(conn, p1)
    p2 = select_drill(conn, "hard_mixed", count=6, seed="same")
    sid2 = persist_session(conn, p2)
    assert sid1 != sid2
    rows = conn.execute(
        "SELECT id, status FROM sessions WHERE id IN (?,?)", (sid1, sid2)
    ).fetchall()
    assert len(rows) == 2  # first session not clobbered


def test_transfer_fallback_excludes_memorized_errors(db):
    conn = _seed(db, n_hist_wrong=30, n_hist_right=2, n_fresh=2)
    wrong_ids = {r[0] for r in conn.execute(
        "SELECT question_id FROM attempts WHERE correct=0 AND mode='historical'")}
    plan = select_drill(conn, "transfer_drill", count=25, seed="over")
    ids = {i["question_id"] for i in plan["items"]}
    assert not ids & wrong_ids
