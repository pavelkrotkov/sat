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
            "rationale_paragraphs": _paragraphs(question.rationale),
            "rationale_is_official": bool(question.rationale),
            "passage_skeleton": skeleton,
            "lesson": lesson,
            "lesson_source": "derived rule" if lesson else "",
        })
    return out


def _infer_trap(tags):
    return tags[:2]


# Periods that are not sentence boundaries. Protected before splitting so
# they do not end the excerpt mid-sentence (PR-50 review findings):
#   - title abbreviations (Dr., Mr., ...) and always-nonterminal ones
#     (vs., e.g., i.e.) are protected unconditionally — in the corpus
#     (1,965 rationales) these never occur sentence-final;
#   - other abbreviations (etc., Inc., U.S., Co., Jr., Sr., ...) are
#     non-terminal only when a lowercase continuation follows, so
#     "..., etc. Choice B...", "Acme Inc. Choice B..." and
#     "Martin Luther King Jr. Choice B..." still split at the real
#     sentence end.
# Name initials ("J. K. Rowling") are deliberately not special-cased:
# they do not occur in the corpus, and a lone capital-period there would
# be indistinguishable from an answer label ("The correct answer is A.").
_TITLE_ABBREVIATIONS = ("Dr.", "Mr.", "Mrs.", "Ms.", "St.")
_ALWAYS_ABBREVIATIONS = ("e.g.", "i.e.", "vs.")
_OTHER_ABBREVIATIONS = ("etc.", "Inc.", "Co.", "Jr.", "Sr.", "U.S.",
                        "U.K.", "A.D.", "B.C.", "Ph.D.", "M.D.")
# Ellipsis runs (compact "...", spaced ". . .", ".. ..") are protected
# only when a lowercase continuation follows — in the corpus (1,965
# rationales, 257 ellipsis occurrences) every ellipsis is mid-sentence
# inside a quote ("In...walls"). A sentence-final ellipsis before a
# capitalized sentence ("inconclusive... Choice B") stays a boundary
# (PR-50 round-8 finding), mirroring the etc./Inc. conditional rule.
_ELLIPSIS = re.compile(r"\.(?:\s*\.)+(?=\s+[a-z])")
# Sentence terminator, optional closing quotes/brackets, then whitespace.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])([\"'\u201d\u2019)\]]*)\s+")


def _protect_abbreviations(text: str) -> str:
    """Replace non-boundary periods with a placeholder so the sentence
    splitter skips them; restored before returning the excerpt.

    Title and always-nonterminal abbreviations are matched
    case-insensitively so capitalized variants ("E.g. the evidence...",
    "I.E. this means") are protected too (PR-50 round-9 finding); the
    conditional list keeps exact-case matching because its entries are
    already case-distinctive (Inc. vs inc. is not a boundary question).
    """
    text = _ELLIPSIS.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    for abbr in _TITLE_ABBREVIATIONS + _ALWAYS_ABBREVIATIONS:
        # protect only the dots in the matched abbreviation, preserving
        # the original casing ("E.g." stays "E.g." after restore)
        pattern = re.compile(re.escape(abbr), re.IGNORECASE)
        text = pattern.sub(lambda m: m.group(0).replace(".", "\x00"), text)
    for abbr in _OTHER_ABBREVIATIONS:
        text = re.sub(re.escape(abbr) + r"(?=\s+[a-z])",
                      abbr.replace(".", "\x00"), text)
    return text


def _split_sentences(text: str) -> list[str]:
    """Split on sentence terminators, keeping closing quotes/brackets with
    the sentence they close."""
    parts = _SENTENCE_SPLIT.split(_protect_abbreviations(text))
    sentences: list[str] = []
    for i in range(0, len(parts), 2):
        sentences.append(parts[i] + (parts[i + 1] if i + 1 < len(parts) else ""))
    return sentences


def excerpt_sentences(text: str, max_chars: int) -> str:
    """Whole sentences from the start of ``text``, capped at ``max_chars``.

    A preview never cuts mid-sentence when a shorter sentence boundary
    exists; a single over-long sentence is cut at a word boundary. Common
    abbreviations and initials (``e.g.``, ``Dr.``, ``U.S.``) are not
    treated as boundaries, and closing quotes/brackets stay attached to
    the sentence they close. The authoritative text itself is always
    rendered in full elsewhere — this is only for compact excerpts.
    """
    if not text:
        return ""
    sentences = _split_sentences(text.strip())
    excerpt: list[str] = []
    total = 0
    for s in sentences:
        # total already includes the separator after the previous sentence,
        # so a sentence that would land the joined excerpt exactly on
        # max_chars still fits (PR-50 round-5 finding).
        if excerpt and total + len(s) > max_chars:
            break
        excerpt.append(s)
        total += len(s) + 1
    out = " ".join(excerpt).replace("\x00", ".")
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0]
    return out.strip()


def _why_key_works(rationale: str) -> str:
    """First paragraph of the official rationale: why the key works."""
    if not rationale:
        return ""
    return excerpt_sentences(rationale.split("\n")[0], max_chars=600)


def _paragraphs(text: str | None) -> list[str]:
    """Non-empty paragraphs of a stored text, preserving author boundaries.

    Rationales are stored newline-separated. Rendering each paragraph as
    its own block keeps the official text intact — no character-level
    truncation anywhere on the review page. A NULL/empty rationale
    (the column is nullable) yields no paragraphs (PR-50 round-6
    finding): the template's ``{% if r.rationale_official %}`` guard
    handles the absence.
    """
    if not text:
        return []
    return [p.strip() for p in text.split("\n") if p.strip()]


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

