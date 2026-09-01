"""Session lifecycle: record attempts, score, build review payloads."""

import json
import re
import statistics

from .. import config
from ..clock import utc_now
from ..ids import opaque_id
from ..corpus.ingest import mark_benchmark_seen
from .sampler import persist_session, select_drill
from .spacing import update_after_attempt
from .weakness import compute_weakness
from ..corpus.tagger import diagnose_attempt
from ..corpus.questions import load, load_many
from ..corpus.tags import tags_by_question


def create_session(conn, mode: str, count: int | None = None, seed: str | None = None,
                   focus_tag: str | None = None) -> dict:
    plan = select_drill(conn, mode, count=count, seed=seed, focus_tag=focus_tag)
    if not plan["session_id"]:
        # benchmark plans have no seed to derive an id from
        plan["session_id"] = opaque_id()
    persist_session(conn, plan)
    by_id = load_many(conn, (item["question_id"] for item in plan["items"]))
    questions = []
    for item in plan["items"]:
        q = by_id.get(item["question_id"])
        if q is None:
            continue
        questions.append({
            "id": q.id,
            "passage": q.passage,
            "stem": q.stem or "Select the best answer.",
            "choices": [c.as_dict() for c in q.choices],
            "images": list(q.images),
            "visuals": [dict(v) for v in q.visuals],
        })
    return {"plan": plan, "questions": questions}


def submit_answer(conn, session_id: str, question_id: int, chosen_letter: str,
                  confidence: int, time_ms: int) -> dict:
    """Record one attempt; returns {'correct', 'key'} without revealing more."""
    sess = conn.execute("SELECT status, plan_json, mode FROM sessions WHERE id=?", (session_id,)).fetchone()
    if sess is None:
        raise ValueError(f"Unknown session {session_id}")
    if sess["status"] != "open":
        raise ValueError(f"Session {session_id} is not open")
    plan_ids = {item["question_id"] for item in json.loads(sess["plan_json"])}
    if question_id not in plan_ids:
        raise ValueError(f"Question {question_id} is not part of session {session_id}")
    prior = conn.execute(
        "SELECT id, correct, chosen_letter FROM attempts WHERE session_id=? AND question_id=?",
        (session_id, question_id),
    ).fetchone()
    if prior is not None:
        # retried submission: never double-count attempts or spacing updates
        return {"correct": bool(prior["correct"]), "key": "",
                "error_tags": [], "duplicate": True}
    q = load(conn, question_id)
    if q is None:
        raise ValueError(f"Question {question_id} not found")
    correct = 1 if q.is_correct_answer(chosen_letter) else 0
    confidence = max(1, min(3, int(confidence)))
    cur = conn.execute(
        """INSERT INTO attempts (session_id, question_id, chosen_letter, correct,
                                 confidence, time_ms, mode, attempted_at)
           VALUES (?,?,?,?,?,?,(SELECT mode FROM sessions WHERE id=?),?)""",
        (session_id, question_id, chosen_letter[:1].upper(), correct, confidence,
         time_ms, session_id, utc_now()),
    )
    update_after_attempt(conn, question_id, correct, confidence)

    error_tags = []
    if q.pool == "protected_benchmark":
        mark_benchmark_seen(conn, [question_id])
    elif not correct:
        error_tags = diagnose_attempt(conn, question_id, [c.as_dict() for c in q.choices],
                                      q.correct_letter, chosen_letter[:1].upper())
        # spec section 6/13: diagnoses belong to THIS attempt, so older
        # reviews never inherit a later attempt's trap analysis
        conn.execute("UPDATE attempts SET error_tags=? WHERE id=?",
                     (json.dumps(error_tags), cur.lastrowid))
    return {"correct": bool(correct), "key": q.correct_letter, "error_tags": error_tags}


def answer_feedback(conn, session_id: str, question_id: int) -> dict | None:
    """What to show in the moment right after one answer.

    The drill used to withhold right/wrong until `/review/{sid}` at the end of
    the session, which is the wrong moment for learning: the recall is
    strongest immediately after committing to a choice. This assembles the
    same material `review_payload` does, for a single question and regardless
    of whether it was correct.

    Returns None when the question has not been answered in this session, so
    the caller can send the student back to it rather than reveal a key for
    an answer never given.
    """
    row = conn.execute(
        """SELECT chosen_letter, correct, confidence FROM attempts
           WHERE session_id=? AND question_id=?""",
        (session_id, question_id),
    ).fetchone()
    if row is None:
        return None
    question = load(conn, question_id)
    if question is None:
        return None
    return {
        "question_id": question_id,
        "correct": bool(row["correct"]),
        "confidence": row["confidence"],
        "chosen_letter": row["chosen_letter"],
        "chosen_text": question.text_of(row["chosen_letter"]),
        "key_letter": question.correct_letter,
        "key_text": question.text_of(question.correct_letter),
        "why_key_works": _why_key_works(question.rationale),
        "official_skill": question.official_skill,
        "visuals": [dict(v) for v in question.visuals],
        "streak": current_streak(conn, session_id),
    }


def current_streak(conn, session_id: str) -> int:
    """Consecutive correct answers ending at the most recent attempt.

    A 4px progress bar was the only signal across a 27-question module. This
    is the cheap counter that makes a good run visible while it is happening.
    """
    rows = conn.execute(
        "SELECT correct FROM attempts WHERE session_id=? ORDER BY id DESC",
        (session_id,),
    ).fetchall()
    streak = 0
    for r in rows:
        if not r["correct"]:
            break
        streak += 1
    return streak


def complete_session(conn, session_id: str) -> dict:
    conn.execute("UPDATE sessions SET status='completed' WHERE id=?", (session_id,))
    rows = conn.execute(
        """SELECT a.*, q.correct_letter FROM attempts a
           JOIN questions q ON q.id=a.question_id WHERE a.session_id=?""",
        (session_id,),
    ).fetchall()
    times = [r["time_ms"] for r in rows if r["time_ms"]]
    result = {
        "total": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "median_time_s": round(statistics.median(times) / 1000, 1) if times else None,
        "confident_wrong": sum(1 for r in rows if not r["correct"] and r["confidence"] >= 3),
        "low_conf_right": sum(1 for r in rows if r["correct"] and r["confidence"] <= 2),
    }
    compute_weakness(conn)  # refresh profile immediately after session
    return result


def review_payload(conn, session_id: str) -> list[dict]:
    """Rich per-question review for incorrect or low-confidence answers."""
    rows = conn.execute(
        "SELECT * FROM attempts WHERE session_id=? ORDER BY id", (session_id,)
    ).fetchall()
    questions = load_many(conn, {r["question_id"] for r in rows})
    tags_by_q = tags_by_question(conn, {r["question_id"] for r in rows})
    err_rows = conn.execute(
        """SELECT et.question_id, et.tag FROM student_error_tags et
           JOIN attempts a ON a.question_id=et.question_id WHERE a.session_id=?""",
        (session_id,),
    ).fetchall()
    errs_by_q: dict[int, list[str]] = {}
    for r in err_rows:
        errs_by_q.setdefault(r["question_id"], []).append(r["tag"])

    out = []
    for r in rows:
        needs_review = (not r["correct"]) or r["confidence"] <= 2
        if not needs_review:
            continue
        question = questions[r["question_id"]]
        tags = tags_by_q.get(r["question_id"], [])
        trap_tags = errs_by_q.get(r["question_id"], []) or _infer_trap(tags)
        lesson = next((config.TAG_LESSONS[t] for t in trap_tags if t in config.TAG_LESSONS), "")
        skeleton = _logical_skeleton(question.passage)
        out.append({
            "question_id": r["question_id"],
            "chosen_letter": r["chosen_letter"],
            "chosen_text": question.text_of(r["chosen_letter"]),
            "correct": bool(r["correct"]),
            "confidence": r["confidence"],
            "key_letter": question.correct_letter,
            "key_text": question.text_of(question.correct_letter),
            "why_key_works": _why_key_works(question.rationale),
            "official_skill": question.official_skill,
            "reasoning_tags": tags,
            "trap_tags": trap_tags,
            "rationale_official": question.rationale,
            "rationale_is_official": bool(question.rationale),
            "passage_skeleton": skeleton,
            "visuals": [dict(v) for v in question.visuals],
            "lesson": lesson,
            "lesson_source": "derived rule" if lesson else "",
        })
    return out


def _infer_trap(tags):
    return tags[:2]


def _why_key_works(rationale: str) -> str:
    """First paragraph of the official rationale states why the key works."""
    if not rationale:
        return ""
    first = rationale.split("\n")[0]
    return first[:600]


def _logical_skeleton(passage: str) -> list[str]:
    """Best-effort heuristic logical skeleton; labeled as derived in UI.

    Picks the sentences carrying the argumentative spine: claims, contrasts,
    hypotheses and results.
    """
    sentences = re.split(r"(?<=[.!?])\s+", passage.replace("\n", " "))
    picked = []
    patterns = [
        r"\b(however|but|yet|although|while|instead)\b",
        r"\b(hypothes\w+|predict\w*|expect\w*|theoriz\w*|assum\w*)\b",
        r"\b(found|showed|revealed|observed|demonstrated|data)\w*\b",
        r"\b(therefore|thus|consequently|as a result|hence)\b",
        r"\b(most|many|some|few)\b.*\b(but|however)\b",
    ]
    limit = 4
    for s in sentences:
        low = s.lower()
        if any(re.search(p, low) for p in patterns):
            cleaned = s.strip()
            if cleaned and cleaned not in picked:
                picked.append(cleaned)
        if len(picked) >= limit:
            break
    if not picked and sentences:
        picked = [sentences[0].strip()]
    return picked

