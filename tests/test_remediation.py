"""Tests for personalized remediation from error patterns (issue #38)."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from satprep.training.remediation import (
    _recommended_tags,
    build_remediation_plan,
    compute_patterns,
    explain_selection,
    measure_improvement,
)


@pytest.fixture
def memdb():
    """In-memory DB for pure aggregation tests (no sampler)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE questions (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_test TEXT DEFAULT '',
        source_question_number TEXT DEFAULT '',
        module TEXT DEFAULT '',
        passage TEXT DEFAULT '',
        stem TEXT DEFAULT '',
        choices_json TEXT NOT NULL DEFAULT '[]',
        correct_letter TEXT NOT NULL,
        rationale TEXT DEFAULT '',
        images_json TEXT DEFAULT '[]',
        official_domain TEXT DEFAULT '',
        official_skill TEXT DEFAULT '',
        skill_source TEXT DEFAULT 'unknown',
        difficulty TEXT DEFAULT '',
        pool TEXT NOT NULL,
        seen_benchmark INTEGER NOT NULL DEFAULT 0,
        is_new_bank INTEGER NOT NULL DEFAULT 0,
        import_batch TEXT DEFAULT '',
        imported_at TEXT NOT NULL,
        provenance_json TEXT DEFAULT '{}',
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE question_tags (
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'rule',
        created_at TEXT NOT NULL,
        UNIQUE(question_id, tag)
    );
    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY,
        session_id TEXT NOT NULL,
        question_id INTEGER NOT NULL REFERENCES questions(id),
        chosen_letter TEXT DEFAULT '',
        correct INTEGER NOT NULL,
        confidence INTEGER DEFAULT 0,
        time_ms INTEGER DEFAULT 0,
        mode TEXT DEFAULT '',
        attempted_at TEXT NOT NULL,
        error_tags TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE question_state (
        question_id INTEGER PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
        times_seen INTEGER NOT NULL DEFAULT 0,
        times_correct INTEGER NOT NULL DEFAULT 0,
        times_wrong INTEGER NOT NULL DEFAULT 0,
        confident_wrong_streak INTEGER NOT NULL DEFAULT 0,
        interval_days REAL NOT NULL DEFAULT 1.0,
        ease REAL NOT NULL DEFAULT 2.5,
        due_at TEXT,
        last_attempted_at TEXT
    );
    CREATE TABLE effective_question_tags (
        question_id INTEGER NOT NULL,
        tag TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'rule',
        created_at TEXT NOT NULL,
        UNIQUE(question_id, tag)
    );
    CREATE TABLE student_error_tags (
        question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        diagnosis_source TEXT NOT NULL DEFAULT 'rule',
        created_at TEXT NOT NULL,
        UNIQUE(question_id, tag)
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        seed TEXT NOT NULL,
        algo_version TEXT NOT NULL,
        plan_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'open'
    );
    CREATE TABLE weakness_cache (
        entity_type TEXT NOT NULL,
        entity TEXT NOT NULL,
        score REAL NOT NULL,
        stats_json TEXT DEFAULT '{}',
        computed_at TEXT NOT NULL,
        PRIMARY KEY (entity_type, entity)
    );
    """)
    conn.commit()
    yield conn
    conn.close()


def _add_question(conn, qid, *, pool="fresh_training", difficulty="medium",
                  skill="Inferences", stem="q", correct="B"):
    conn.execute(
        """INSERT INTO questions (id, fingerprint, source, passage, stem,
             choices_json, correct_letter, official_skill, difficulty, pool,
             imported_at)
           VALUES (?, ?, 'college_board_question_bank', 'passage', ?,
             '["A","B","C","D"]', ?, ?, ?, ?, '2026-01-01')""",
        (qid, f"fp-{qid}", stem, correct, skill, difficulty, pool),
    )


def _add_attempt(conn, qid, *, correct=0, confidence=3, days_ago=5,
                 error_tags=("qualifier_strength",), per_attempt=None):
    when = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    # per_attempt (when not None) overrides the student_error_tags snapshot
    # for THIS row only — simulates the per-attempt JSON written by
    # the live session pipeline. Falls back to the legacy INSERT into
    # student_error_tags otherwise.
    if per_attempt is not None:
        import json as _json
        et_json = _json.dumps(list(per_attempt))
    else:
        et_json = "[]"
    conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter,
             correct, confidence, attempted_at, error_tags)
           VALUES ('s', ?, 'A', ?, ?, ?, ?)""",
        (qid, correct, confidence, when, et_json),
    )
    if per_attempt is None:
        for t in error_tags:
            conn.execute(
                """INSERT OR REPLACE INTO student_error_tags
                     (question_id, tag, created_at) VALUES (?, ?, '2026-01-01')""",
                (qid, t),
            )
    else:
        for t in per_attempt:
            conn.execute(
                """INSERT OR REPLACE INTO student_error_tags
                     (question_id, tag, created_at) VALUES (?, ?, '2026-01-01')""",
                (qid, t),
            )
    conn.commit()


def _add_question_state(conn, qid):
    conn.execute(
        "INSERT INTO question_state (question_id) VALUES (?)", (qid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Pattern aggregation
# ---------------------------------------------------------------------------

def test_compute_patterns_aggregates_error_tags(memdb):
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",))
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",))
    _add_attempt(memdb, 2, error_tags=("cause_vs_correlation",))
    patterns = compute_patterns(memdb)
    by_tag = {p.tag: p for p in patterns}
    assert "qualifier_strength" in by_tag
    assert by_tag["qualifier_strength"].evidence_count == 2
    assert by_tag["qualifier_strength"].status == "ok"
    # Sorted by score desc (higher recent error rate → higher priority)
    assert patterns[0].score >= patterns[-1].score
    # KB tactics linked for known tags
    assert by_tag["qualifier_strength"].kb_tactic_refs == \
        ["kb/wiki/summaries/settele-strong-words.md"]
    assert by_tag["cause_vs_correlation"].kb_tactic_refs == \
        ["kb/wiki/summaries/settele-dumb-summaries.md"]


def test_compute_patterns_recent_weighting(memdb):
    _add_question(conn=memdb, qid=1)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=60)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(memdb)
    p = patterns[0]
    # Recent window only counts the 2-day-old attempt.
    assert p.recent_total == 1
    assert p.recent_wrong == 1


def test_cold_start_sparse_conflicting_status(memdb):
    _add_question(conn=memdb, qid=1)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(memdb)
    assert patterns[0].status == "sparse"   # single attempt


# ---------------------------------------------------------------------------
# Improvement measurement
# ---------------------------------------------------------------------------

def test_measure_improvement_detects_improvement(memdb):
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=45)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=45)
    _add_attempt(memdb, 2, error_tags=("qualifier_strength",), days_ago=3, correct=1)
    patterns = compute_patterns(memdb)
    impr = measure_improvement(memdb, patterns)
    d = impr["qualifier_strength"]
    assert d["older_error_rate"] == 1.0     # both old attempts wrong
    assert d["recent_error_rate"] == 0.0    # recent attempt correct
    assert d["delta"] < 0                   # improved


def test_measure_improvement_no_older_data(memdb):
    _add_question(conn=memdb, qid=1)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(memdb)
    impr = measure_improvement(memdb, patterns)
    assert impr["qualifier_strength"]["older_error_rate"] is None


# ---------------------------------------------------------------------------
# Recommended tags
# ---------------------------------------------------------------------------

def test_recommended_tags_skips_cold_start_noise(db):
    """One cold-start tag with a single attempt should not be recommended
    at high priority."""
    from conftest import add_question
    conn, _ = db
    add_question(conn, stem="q1", correct="B")
    qid = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()[0]
    _add_attempt(conn, qid, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(conn)
    tags = _recommended_tags(patterns, min_score=0)
    assert tags == []   # cold_start with <3 evidence excluded


# ---------------------------------------------------------------------------
# Full plan + explainability + benchmark safety (real project schema)
# ---------------------------------------------------------------------------

def test_build_remediation_plan_no_match_is_explicit(db):
    """No matching questions -> drill None, recommended_tags empty,
    explain_selection says so. Never a crash."""
    conn, _ = db
    # No attempts at all -> no error patterns -> no tags -> no drill.
    plan = build_remediation_plan(conn, count=4)
    assert plan.patterns == []
    assert plan.recommended_tags == []
    assert plan.drill is None
    lines = explain_selection(plan)
    assert any("no matching" in l for l in lines)


def test_build_remediation_plan_with_weak_pattern(db):
    from conftest import add_question
    conn, _ = db
    add_question(conn, stem="q1", correct="B", tags=("qualifier_strength",))
    add_question(conn, stem="q2", correct="B", tags=("qualifier_strength",))
    add_question(conn, stem="q3", correct="B", tags=("qualifier_strength",))
    qids = [r["id"] for r in conn.execute("SELECT id FROM questions").fetchall()]
    for q in qids:
        _add_attempt(conn, q, error_tags=("qualifier_strength",))
    plan = build_remediation_plan(conn, count=4)
    assert plan.patterns
    assert "qualifier_strength" in plan.recommended_tags
    assert plan.drill is not None
    assert len(plan.drill["items"]) > 0
    # And every drill item should expose at least one explainable
    # weak-tag / remediation reason in `why`.
    for item in plan.drill["items"]:
        labels = [label for label, _ in (item.get("why") or [])]
        assert any(l.startswith("weak-tag:") or l.startswith("remediation:")
                   for l in labels), \
            f"expected explainable reason in {labels}"
    lines = explain_selection(plan)
    assert any("selected because" in l for l in lines)


def test_remediation_never_touches_protected_benchmark(db):
    """The remediation mode must never select protected_benchmark items
    — the pool filter in pools_for('remediation') excludes it
    structurally. This is the leakage invariant for #38."""
    from conftest import add_question

    from satprep.training.composition import pools_for
    conn, _ = db
    assert "protected_benchmark" not in pools_for("remediation")
    # And the sampler's plan can never carry a protected item.
    add_question(conn, stem="q1", correct="B")
    add_question(conn, stem="q2", correct="B")
    add_question(conn, stem="q3", correct="B", pool="protected_benchmark")
    qids = [r["id"] for r in conn.execute("SELECT id FROM questions").fetchall()]
    for q in qids:
        _add_attempt(conn, q, error_tags=("qualifier_strength",))
    plan = build_remediation_plan(conn, count=4)
    if plan.drill:
        for item in plan.drill["items"]:
            assert item.get("bucket") != "protected_unseen"


def test_module_boundary_remediation_imports_trainer_only():
    import satprep.training.remediation as rem
    with open(rem.__file__) as fh:
        src = fh.read()
    # It lives in training/ and may import from training, but must not
    # import server/analytics/cli (those reach back up).
    assert "from ..server" not in src
    assert "from ..cli" not in src
    assert "from ..analytics" not in src


# ---------------------------------------------------------------------------
# Regression coverage for review-round-1 findings (issue #38 PR #45)
# ---------------------------------------------------------------------------

def test_error_tag_attempts_includes_question_id(memdb):
    """P1: question_id must be selectable from _error_tag_attempts so the
    conflicting status classifier sees real question_ids (not None)."""
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",))
    _add_attempt(memdb, 2, error_tags=("qualifier_strength",))
    from satprep.training.remediation import _error_tag_attempts
    per_tag = _error_tag_attempts(memdb)
    qids = {r["question_id"] for rows in per_tag.values() for r in rows}
    assert qids == {1, 2}


def test_pattern_status_conflicting_only_when_real(memdb):
    """P1: with two distinct questions on the same tag, status must be
    'ok', not 'conflicting'. The old code always reported 'conflicting'
    because question_id was missing from the SELECT."""
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",))
    _add_attempt(memdb, 2, error_tags=("qualifier_strength",))
    patterns = compute_patterns(memdb)
    assert patterns[0].status == "ok"


def test_recent_error_rate_is_real_ratio_not_1(memdb):
    """P1: 3 wrong + 7 correct recent attempts must NOT collapse to
    rate 1.0. Old code filtered out correct attempts before computing
    the rate, so any tag with a recent wrong answer always reported 1.0.
    """
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    # 3 wrong on q1, 7 correct on q2 — all within the 30-day window.
    for _ in range(3):
        _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=2)
    for _ in range(7):
        _add_attempt(memdb, 2, error_tags=("qualifier_strength",),
                     days_ago=2, correct=1)
    patterns = compute_patterns(memdb)
    p = next(pp for pp in patterns if pp.tag == "qualifier_strength")
    assert p.recent_total == 10
    assert p.recent_wrong == 3
    assert abs(p.recent_error_rate - 0.3) < 1e-6


def test_per_attempt_error_tags_drive_aggregation(memdb):
    """P1: per-attempt error_tags (written to attempts.error_tags JSON)
    must drive aggregation, not the question-level student_error_tags
    snapshot. A correct attempt's per-attempt tags should be visible."""
    _add_question(conn=memdb, qid=1)
    _add_question(conn=memdb, qid=2)
    # q1's per-attempt diagnosis is "qualifier_strength"; q2 is unrelated.
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",),
                 per_attempt=("qualifier_strength",), days_ago=2)
    # Per-attempt tag differs from the snapshot — must use the JSON.
    _add_attempt(memdb, 2, error_tags=("legacy_tag",),
                 per_attempt=("over_inference",), days_ago=2)
    patterns = compute_patterns(memdb)
    tags = {p.tag for p in patterns}
    assert "over_inference" in tags
    assert "legacy_tag" not in tags  # snapshot alone is no longer authoritative


def test_explain_selection_reads_sampler_why(db):
    """P2: explain_selection must read the sampler's `why` field, not
    `score_breakdown`. Old code always reported 'general weakness match'."""
    from conftest import add_question
    conn, _ = db
    # Seed demand-tag rows so the candidate's `why` carries a weak-tag
    # component for the explanation to surface (without any demand-tag
    # match, every item legitimately falls back to general match).
    add_question(conn, stem="q1", correct="B", tags=("qualifier_strength",))
    add_question(conn, stem="q2", correct="B", tags=("qualifier_strength",))
    add_question(conn, stem="q3", correct="B", tags=("qualifier_strength",))
    qids = [r["id"] for r in conn.execute("SELECT id FROM questions").fetchall()]
    for q in qids:
        _add_attempt(conn, q, error_tags=("qualifier_strength",))
    plan = build_remediation_plan(conn, count=4)
    assert plan.drill is not None
    items = plan.drill["items"]
    # Each item must carry an explainable `why` list of (label, value) pairs
    # (not a `score_breakdown` dict).
    assert items, "expected at least one drill item"
    assert all(isinstance(it.get("why"), list) for it in items), \
        f"each item must store why as a list: {items[0]}"
    lines = explain_selection(plan)
    # At least one item should expose a real reason rather than the fallback.
    non_fallback = [l for l in lines if "general weakness match" not in l]
    assert non_fallback, f"all explanations fell back to 'general weakness match': {lines}"


def test_drill_none_when_no_eligible_question(db):
    """P2: build_remediation_plan with focus_tag and NO matching question
    must return drill=None (documented contract), not a non-null dict
    whose items came purely from the sampler's FALLBACK_BUCKET.

    With no attempts, no error-tag profile exists, so the
    remediation-specific boost yields nothing. The sampler still
    produces a non-null dict with generic candidates; we collapse
    that to honour the documented contract.
    """
    from conftest import add_question
    conn, _ = db
    add_question(conn, stem="q1", correct="B")
    # No attempts → no pattern → focus_tag still triggers select_drill,
    # but no candidate carries the tag.
    plan = build_remediation_plan(conn, count=4, focus_tag="qualifier_strength")
    assert plan.patterns == []
    # With no error-tag profile, every drill item would be a generic
    # fallback (no remediation component). The documented contract is
    # drill=None in that case, which explain_selection already reports.
    assert plan.drill is None, (
        f"focus-tag drill with no eligible remediation candidates must be None, "
        f"got drill with items: {plan.drill.get('items') if plan.drill else None}"
    )
    lines = explain_selection(plan)
    assert any("no matching" in l for l in lines)


def test_measure_improvement_delta_none_for_empty_windows(db):
    """P2: when either window is empty, delta must be None, not a
    misleading 0.0 / recent_rate. Both empty-older and empty-recent
    cases must withhold."""
    from conftest import add_question
    conn, _ = db
    add_question(conn, stem="q1", correct="B")
    qid = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()[0]
    # recent-only attempt (days_ago=2, so < 30d window)
    _add_attempt(conn, qid, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(conn)
    impr = measure_improvement(conn, patterns)
    d = impr["qualifier_strength"]
    assert d["older_error_rate"] is None
    assert d["delta"] is None


def test_select_drill_accepts_now_for_reproducibility(db):
    """P2: select_drill must honor a caller-supplied `now` so that
    remediation plans that capture a reference time don't have the
    drill selection drift to wall-clock."""
    from conftest import add_question

    from satprep.training.sampler import select_drill
    conn, _ = db
    add_question(conn, stem="q1", correct="B")
    add_question(conn, stem="q2", correct="B")
    add_question(conn, stem="q3", correct="B")
    past = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    # Two calls with the same past `now` should agree on item set/ordering.
    a = select_drill(conn, "targeted_drill", count=12, seed="x", now=past)
    b = select_drill(conn, "targeted_drill", count=12, seed="x", now=past)
    assert [i["question_id"] for i in a["items"]] == \
           [i["question_id"] for i in b["items"]]


def test_deactivated_question_excluded_from_remediation_evidence(memdb):
    """P2: attempts on a deactivated (active=0) question must NOT
    contribute to remediation evidence — only active questions count."""
    # Same qid, but mark active=0 after the row exists.
    _add_question(conn=memdb, qid=1)
    memdb.execute("UPDATE questions SET active=0 WHERE id=1")
    _add_attempt(memdb, 1, error_tags=("qualifier_strength",), days_ago=2)
    patterns = compute_patterns(memdb)
    # The attempt exists, but the question is deactivated → no evidence.
    assert patterns == [] or all(p.evidence_count == 0 for p in patterns)


def test_improvement_includes_correctly_answered_transfer(db):
    """P1: a correctly-answered question that shares a demand tag with
    a diagnosed question must contribute improvement evidence. Old
    code only joined on student_error_tags, so transfer-correct
    answers were invisible to the metric."""
    from conftest import add_question
    conn, _ = db
    add_question(conn, stem="q1", correct="B")  # transfer target
    add_question(conn, stem="q2", correct="B")
    add_question(conn, stem="q3", correct="B")
    # q1 has the diagnosis; q2 shares a demand tag in question_tags
    # (effective_question_tags is a view over this in production).
    conn.execute("""INSERT INTO question_tags (question_id, tag, origin, created_at)
                    VALUES (1, 'inference', 'rule', '2026-01-01'),
                           (2, 'inference', 'rule', '2026-01-01')""")
    conn.commit()
    _add_attempt(conn, 1, error_tags=("qualifier_strength",), days_ago=45)
    _add_attempt(conn, 2, error_tags=("qualifier_strength",), days_ago=3,
                 correct=1, per_attempt=())  # no per-attempt tag for transfer
    patterns = compute_patterns(conn)
    impr = measure_improvement(conn, patterns)
    d = impr.get("qualifier_strength", {})
    # recent_n now includes q2's correct transfer attempt (old code missed it).
    assert d.get("recent_n", 0) >= 1