"""Tests for personalized remediation from error patterns (issue #38)."""
from __future__ import annotations

import datetime
import sqlite3

import pytest

from satprep.training.remediation import (
    ErrorPattern,
    RemediationPlan,
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
                 error_tags=("qualifier_strength",)):
    when = (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter,
             correct, confidence, attempted_at, error_tags)
           VALUES ('s', ?, 'A', ?, ?, ?, ?)""",
        (qid, correct, confidence, when, "[]"),
    )
    for t in error_tags:
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
    conn, path = db
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
    conn, path = db
    # No attempts at all -> no error patterns -> no tags -> no drill.
    plan = build_remediation_plan(conn, count=4)
    assert plan.patterns == []
    assert plan.recommended_tags == []
    assert plan.drill is None
    lines = explain_selection(plan)
    assert any("no matching" in l for l in lines)


def test_build_remediation_plan_with_weak_pattern(db):
    from conftest import add_question
    conn, path = db
    add_question(conn, stem="q1", correct="B")
    add_question(conn, stem="q2", correct="B")
    add_question(conn, stem="q3", correct="B")
    qids = [r["id"] for r in conn.execute("SELECT id FROM questions").fetchall()]
    for q in qids:
        _add_attempt(conn, q, error_tags=("qualifier_strength",))
    plan = build_remediation_plan(conn, count=4)
    assert plan.patterns
    assert "qualifier_strength" in plan.recommended_tags
    assert plan.drill is not None
    assert len(plan.drill["items"]) > 0
    lines = explain_selection(plan)
    assert any("selected because" in l for l in lines)


def test_remediation_never_touches_protected_benchmark(db):
    """The remediation mode must never select protected_benchmark items
    — the pool filter in pools_for('remediation') excludes it
    structurally. This is the leakage invariant for #38."""
    from conftest import add_question
    from satprep.training.composition import pools_for
    conn, path = db
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
    src = open(rem.__file__).read()
    # It lives in training/ and may import from training, but must not
    # import server/analytics/cli (those reach back up).
    assert "from ..server" not in src
    assert "from ..cli" not in src
    assert "from ..analytics" not in src