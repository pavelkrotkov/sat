from satprep.sampler import select_drill
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
    return path


def test_deterministic_for_same_seed(db):
    conn, _ = db
    path = _seed(db)
    a = select_drill("targeted_drill", count=12, seed="fixed", db_path=path)
    b = select_drill("targeted_drill", count=12, seed="fixed", db_path=path)
    assert [i["question_id"] for i in a["items"]] == [i["question_id"] for i in b["items"]]


def test_plan_has_explainable_components(db):
    path = _seed(db)
    plan = select_drill("targeted_drill", count=6, seed="x", db_path=path)
    assert plan["items"]
    for item in plan["items"]:
        assert item["why"], "every selection must record its reasons"
        labels = {lbl for lbl, _ in item["why"]}
        assert any(l.startswith(("weak-tag", "skill-weakness", "difficulty", "base")) for l in labels)


def test_targeted_mix_includes_old_wrong_and_fresh(db):
    path = _seed(db)
    plan = select_drill("targeted_drill", count=12, seed="mix", db_path=path)
    buckets = {i["bucket"] for i in plan["items"]}
    assert "old_wrong_due" in buckets
    assert "fresh_weak" in buckets
    assert len(plan["items"]) == 12
    ids = [i["question_id"] for i in plan["items"]]
    assert len(ids) == len(set(ids)), "no duplicate questions within a drill"


def test_transfer_mode_excludes_previously_wrong(db):
    path = _seed(db)
    from satprep.db import connect
    c = connect(path)
    wrong_ids = {r[0] for r in c.execute(
        """SELECT question_id FROM attempts WHERE correct=0 AND mode='historical'""")}
    c.close()
    plan = select_drill("transfer_drill", count=10, seed="t", db_path=path)
    ids = {i["question_id"] for i in plan["items"]}
    assert not ids & wrong_ids, "transfer drill must not serve memorized errors"


def test_error_clinic_prefers_due_errors(db):
    path = _seed(db)
    plan = select_drill("error_clinic", count=8, seed="e", db_path=path)
    buckets = {i["bucket"] for i in plan["items"]}
    assert "old_wrong_due" in buckets


def test_exposure_penalty_demotes_seen_questions(db):
    from satprep.sampler import Candidate, score_candidate

    conn, _ = db
    path = _seed(db)
    from satprep.db import connect as c2
    c = c2(path)
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
