import json
from pathlib import Path

import pytest

from satprep.db import connect


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "test.db"
    conn = connect(path)
    yield conn, str(path)
    conn.close()


def add_question(conn, *, passage="P", stem="Q?", choices=("a", "b", "c", "d"),
                 correct="A", source="college_board_question_bank", pool=None,
                 difficulty="", skill="", tags=(), fingerprint=None,
                 import_batch="batch1"):
    from satprep import fingerprint as fpmod

    fp = fingerprint or fpmod.fingerprint(passage, stem, list(choices))
    if pool is None:
        pool = "historical" if source == "bluebook_test" else fpmod.pool_for_fingerprint(fp)
    cur = conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             official_domain, official_skill, skill_source, difficulty, pool, seen_benchmark,
             is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)""",
        (fp, source, "SAT Practice Test 1", "1", "Module 1", passage, stem,
         json.dumps([{"letter": chr(65 + i), "text": t, "is_correct": chr(65 + i) == correct}
                     for i, t in enumerate(choices)]),
         correct, "rationale", "[]", "", skill, "metadata" if skill else "unknown",
         difficulty, pool, import_batch if source != "bluebook_test" else "",
         "2026-01-01T00:00:00+00:00", "{}"),
    )
    qid = cur.lastrowid
    conn.execute("INSERT INTO question_state (question_id) VALUES (?)", (qid,))
    for t in tags:
        conn.execute(
            "INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,'rule','2026-01-01')",
            (qid, t),
        )
    return qid


def add_attempt(conn, qid, correct, confidence=2, mode="historical",
                attempted_at="2026-03-01T00:00:00+00:00", session_id="hist:x"):
    conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                 confidence, time_ms, mode, attempted_at)
           VALUES (?,?,?,?,?,0,?,?)""",
        (f"{session_id}:{qid}" if session_id.startswith("hist") else session_id,
         qid, "B", int(correct), int(confidence), mode, attempted_at),
    )
