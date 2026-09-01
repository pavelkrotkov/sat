"""SQLite persistence layer: schema + connection helpers.

Canonical runtime database lives at data/satprep.db. All generated state is
rebuildable from raw sources (outputs/ + artifacts/) plus imports/.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

#: Bumped whenever SCHEMA or _migrate changes. Stamped into PRAGMA
#: user_version so a database swapped in underneath a running process is
#: detected by more than the presence of one table.
SCHEMA_VERSION = 1

#: Join target for tag reads. Defined in SCHEMA; the semantics live in
#: satprep.corpus.tags, which re-exports this name.
EFFECTIVE_TAGS = "effective_question_tags"

#: Connection-scoped pragmas, re-applied to every connection. foreign_keys in
#: particular resets to OFF on each new connection, so it cannot live in
#: SCHEMA now that the DDL runs once per path rather than once per connect.
CONNECTION_PRAGMAS = "PRAGMA foreign_keys=ON;"

SCHEMA = """
PRAGMA journal_mode=WAL;

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
    visuals_json TEXT DEFAULT '[]',     -- first-class non-image visuals (tables)
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
    attempted_at TEXT NOT NULL,
    error_tags TEXT NOT NULL DEFAULT '[]'
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

-- Tag reads go through this view rather than question_tags, so a suppressed
-- tag disappears everywhere at once. The origin vocabulary it encodes belongs
-- to satprep.corpus.tags; the literal here is pinned to tags.ORIGIN_SUPPRESSED
-- by tests/test_tags.py so the two cannot drift.
CREATE VIEW IF NOT EXISTS effective_question_tags AS
    SELECT question_id, tag FROM question_tags WHERE origin != 'suppressed';

CREATE TABLE IF NOT EXISTS llm_tag_cache (
    fingerprint TEXT PRIMARY KEY,
    tags_json TEXT NOT NULL,
    model TEXT NOT NULL,
    evidence_json TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""


#: Paths whose schema this process has already applied. Schema creation is
#: idempotent but not free, and it used to run on every single connect().
_SCHEMA_APPLIED: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection. Prefer `db_context`, which also commits and closes."""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the web UI hands each request its own
    # connection, but FastAPI may create it on a threadpool thread and run the
    # handler on the event loop. One connection is still only ever used by one
    # request, so the guard protects nothing here and only breaks the handoff.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn, path)
    conn.executescript(CONNECTION_PRAGMAS)
    return conn


def _ensure_schema(conn: sqlite3.Connection, path: Path) -> None:
    """Apply the schema once per path per process.

    The version stamp, not the presence of a table, is what says a database is
    current: a file swapped in underneath a running process can carry an older
    or partial schema and still have `questions`. A stamp from the future is
    refused outright - running this build's DDL over it and restamping would
    silently downgrade the marker. The lock serialises
    first-time creation, which is otherwise a race between two concurrent
    requests against a brand-new database - `IF NOT EXISTS` does not stop the
    two DDL scripts from deadlocking on the write lock.
    """
    key = str(path.resolve())
    if key in _SCHEMA_APPLIED and _schema_version(conn) == SCHEMA_VERSION:
        return
    with _SCHEMA_LOCK:
        version = _schema_version(conn)
        if key in _SCHEMA_APPLIED and version == SCHEMA_VERSION:
            return
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database at {path} was written by a newer satprep "
                f"(schema v{version}; this build understands v{SCHEMA_VERSION}). "
                f"Upgrade satprep rather than letting it downgrade the file."
            )
        _apply_schema(conn)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        _SCHEMA_APPLIED.add(key)


def _schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight column migrations for pre-existing databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)")}
    if "error_tags" not in cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN error_tags TEXT NOT NULL DEFAULT '[]'")
    qcols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
    if "visuals_json" not in qcols:
        conn.execute("ALTER TABLE questions ADD COLUMN visuals_json TEXT DEFAULT '[]'")


@contextmanager
def db_context(db_path: Path | str | None = None):
    """One connection, one transaction, for the span of one command.

    Every entry point - each CLI subcommand, each web request - opens exactly
    one of these and passes the connection down. Nothing below the entry point
    opens or closes a connection of its own, so a command that fails partway
    rolls back as a unit instead of leaving half its writes behind.
    """
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        # BaseException, not Exception: a Ctrl-C partway through a drill should
        # still roll back rather than leave the transaction dangling. Nothing
        # is swallowed - the bare `raise` re-raises KeyboardInterrupt and
        # SystemExit unchanged, so termination stays clean.
        conn.rollback()
        raise
    finally:
        conn.close()
