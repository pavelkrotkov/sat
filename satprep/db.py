"""SQLite persistence layer: schema + connection helpers.

Canonical runtime database lives at data/satprep.db. All generated state is
rebuildable from raw sources (outputs/ + artifacts/) plus imports/.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,                  -- bluebook_test | college_board_question_bank | custom_generated
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
    skill_source TEXT DEFAULT 'unknown',   -- metadata | derived | manual | unknown
    difficulty TEXT DEFAULT '',            -- easy|medium|hard|'' (never fabricated)
    pool TEXT NOT NULL,                    -- historical | fresh_training | protected_benchmark
    seen_benchmark INTEGER NOT NULL DEFAULT 0,
    is_new_bank INTEGER NOT NULL DEFAULT 0,
    import_batch TEXT DEFAULT '',
    imported_at TEXT NOT NULL,
    provenance_json TEXT DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS question_tags (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'rule',   -- rule | llm | manual | error_diagnosis
    created_at TEXT NOT NULL,
    UNIQUE(question_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON question_tags(tag);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    question_id INTEGER NOT NULL REFERENCES questions(id),
    chosen_letter TEXT DEFAULT '',
    correct INTEGER NOT NULL,
    confidence INTEGER DEFAULT 0,          -- 1..3; 0 = not collected (historical)
    time_ms INTEGER DEFAULT 0,
    mode TEXT DEFAULT '',
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_q ON attempts(question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_time ON attempts(attempted_at);

CREATE TABLE IF NOT EXISTS question_state (
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

-- Historical error diagnoses live apart from question characteristics (§6).
CREATE TABLE IF NOT EXISTS student_error_tags (
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    diagnosis_source TEXT NOT NULL DEFAULT 'rule',  -- rule | llm | manual
    created_at TEXT NOT NULL,
    UNIQUE(question_id, tag)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seed TEXT NOT NULL,
    algo_version TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open'    -- open | completed | abandoned
);

CREATE TABLE IF NOT EXISTS weakness_cache (
    entity_type TEXT NOT NULL,             -- skill | tag | error_tag
    entity TEXT NOT NULL,
    score REAL NOT NULL,
    stats_json TEXT DEFAULT '{}',
    computed_at TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity)
);

CREATE TABLE IF NOT EXISTS llm_tag_cache (
    fingerprint TEXT PRIMARY KEY,
    tags_json TEXT NOT NULL,
    model TEXT NOT NULL,
    evidence_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def db_context(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
