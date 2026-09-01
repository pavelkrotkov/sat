import re

import pytest

from satprep.training.sessions import (complete_session, create_session,
                                       excerpt_sentences, review_payload,
                                       submit_answer)
from conftest import add_question


def _setup(db):
    conn, path = db
    # historical errors + rights + fresh pool
    wq = []
    for i in range(5):
        qid = add_question(conn, passage=f"wp{i}", stem=f"ws{i}?",
                           choices=[f"a{i}", f"b{i}", f"c{i}", f"d{i}"], correct="A",
                           source="bluebook_test", pool="historical",
                           tags=("qualifier_strength",))
        conn.execute("INSERT INTO attempts (session_id, question_id, chosen_letter, correct, confidence, time_ms, mode, attempted_at) VALUES (?,?,?,?,0,0,'historical','2026-03-01')",
                     (f"sw{i}", qid, "C", 0))
        wq.append(qid)
    for i in range(8):
        add_question(conn, passage=f"rp{i}", stem=f"rs{i}?",
                     choices=["ra", "rb", "rc", "rd"], correct="B",
                     source="bluebook_test", pool="historical", tags=("qualifier_strength",))
    for i in range(10):
        add_question(conn, passage=f"fp{i}", stem=f"fs{i}?",
                     choices=["fa", "fb", "fc", "fd"], correct="D",
                     source="college_board_question_bank", pool="fresh_training",
                     difficulty="hard", tags=("qualifier_strength",))
    conn.commit()
    return conn


def test_full_lifecycle_records_scores_and_review(db):
    conn = _setup(db)
    sess = create_session(conn, "targeted_drill", count=6, seed="life")
    sid = sess["plan"]["session_id"]
    answers = {}
    for idx, q in enumerate(sess["questions"]):
        letter = "A" if idx % 2 == 0 else q["choices"][1]["letter"]  # mix of key/wrong guesses
        conf = 3 if idx % 2 == 0 else 1
        res = submit_answer(conn, sid, q["id"], letter, conf, 42000)
        answers[q["id"]] = res
    summary = complete_session(conn, sid)
    assert summary["total"] == len(sess["questions"])
    assert summary["correct"] == sum(r["correct"] for r in answers.values())
    reviews = review_payload(conn, sid)
    for r in reviews:
        if not r["correct"]:
            assert r["key_letter"] and r["trap_tags"] is not None


def test_benchmark_answer_marks_seen(db):
    from satprep.db import connect

    conn = _setup(db)
    sess = create_session(conn, "fresh_benchmark", count=4, seed="b")
    if not sess["questions"]:
        pytest.skip("no protected items in split")
    sid = sess["plan"]["session_id"]
    q = sess["questions"][0]
    submit_answer(conn, sid, q["id"], "Z", 2, 1000)
    row = conn.execute("SELECT pool, seen_benchmark FROM questions WHERE id=?",
                       (q["id"],)).fetchone()
    assert row["seen_benchmark"] == 1 and row["pool"] != "protected_benchmark"


def test_confidence_clamped(db):
    conn = _setup(db)
    sess = create_session(conn, "error_clinic", count=3, seed="c")
    q = sess["questions"][0]
    submit_answer(conn, sid := sess["plan"]["session_id"], q["id"],
                  sess["questions"][0]["choices"][0]["letter"], 9, 5)
    row = conn.execute("SELECT MAX(confidence) FROM attempts WHERE session_id=?", (sid,)).fetchone()[0]
    assert row <= 3


def test_duplicate_submission_does_not_double_count(db):
    conn = _setup(db)
    sess = create_session(conn, "error_clinic", count=3, seed="dup")
    sid = sess["plan"]["session_id"]
    q = sess["questions"][0]
    r1 = submit_answer(conn, sid, q["id"], "Z", 2, 100)
    r2 = submit_answer(conn, sid, q["id"], "Z", 2, 100)
    assert r2.get("duplicate") is True
    n = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE session_id=? AND question_id=?",
        (sid, q["id"]),
    ).fetchone()[0]
    st = conn.execute("SELECT times_seen FROM question_state WHERE question_id=?", (q["id"],)).fetchone()
    conn.close()
    assert n == 1 and (st is None or st["times_seen"] <= 1)


def test_submission_outside_session_plan_rejected(db):
    from conftest import add_question
    conn = _setup(db)
    sess = create_session(conn, "targeted_drill", count=4, seed="rogue")
    sid = sess["plan"]["session_id"]
    rogue = add_question(db[0], passage="X", stem="x?", choices=["1", "2"], correct="A")
    db[0].commit()
    import pytest
    with pytest.raises(ValueError):
        submit_answer(conn, sid, rogue, "A", 3, 10)


def test_benchmark_release_requires_plan_membership(db):
    """Greptile P1: protected items can only be released via their own session plan."""
    from conftest import add_question
    conn, path = db
    fp = __import__("satprep.corpus.fingerprint", fromlist=["fingerprint"]).fingerprint(
        "prot-p", "prot-s?", ["pa", "pb", "pc", "pd"])
    pid = add_question(conn, passage="prot-p", stem="prot-s?",
                       choices=["pa", "pb", "pc", "pd"],
                       source="college_board_question_bank",
                       pool="protected_benchmark", fingerprint=fp)
    conn.commit()
    sess = create_session(conn, "targeted_drill", count=2, seed="leak")
    sid = sess["plan"]["session_id"]
    import pytest
    with pytest.raises(ValueError):
        submit_answer(conn, sid, pid, "A", 3, 10)
    from satprep.db import connect
    row = connect(path).execute("SELECT pool, seen_benchmark FROM questions WHERE id=?", (pid,)).fetchone()
    assert row["pool"] == "protected_benchmark" and row["seen_benchmark"] == 0


def test_interrupted_drill_rolls_back_as_a_unit(db, tmp_path):
    """A drill used to span five uncoordinated transactions: select_drill,
    persist_session, create_session's own body, each submit_answer and
    complete_session each opened, committed and closed separately. A failure
    partway left a committed session row with orphaned attempts. One
    db_context per command makes the whole drill one unit."""
    import pytest

    from satprep.db import connect, db_context

    conn, path = db
    for i in range(6):
        add_question(conn, passage=f"p{i}", stem=f"s{i}?",
                     choices=[f"c{i}{l}" for l in "abcd"],
                     source="bluebook_test", pool="historical")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="interrupted"):
        with db_context(path) as tx:
            sess = create_session(tx, "error_clinic", count=3, seed="boom")
            sid = sess["plan"]["session_id"]
            submit_answer(tx, sid, sess["questions"][0]["id"], "A", 2, 100)
            raise RuntimeError("interrupted")

    after = connect(path)
    assert after.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM question_state WHERE times_seen > 0").fetchone()[0] == 0
    after.close()


# ------------------------------------------------------- excerpt helpers -- #
# Issue #48: the compact "why the key works" preview must never cut the
# official rationale mid-sentence, and the full rationale is rendered
# verbatim on the review page (see test_drill_ui render tests).

def test_excerpt_sentences_never_cuts_mid_sentence():
    s1 = "Choice B is best because it stays within the passage's scope."
    s2 = "The other choices introduce claims the passage never makes."
    out = excerpt_sentences(f"{s1} {s2} {s2} {s2} {s2} {s2}", max_chars=200)
    # whole sentences only: the last included sentence ends at a boundary
    assert out.endswith(("scope.", "makes."))
    assert out not in (s1, s2)  # a real excerpt, not the whole first sentence alone
    # and it never ends mid-word with a clipped tail
    assert re.search(r"\S\.$", out)


def test_excerpt_sentences_single_overlong_sentence_cuts_at_word_boundary():
    long = "word " * 300 + "tail."
    out = excerpt_sentences(long, max_chars=600)
    assert len(out) <= 600
    assert not out.endswith(" ")           # no dangling space at the cut
    assert out.count(" ") > 0              # word boundary kept, not mid-word


def test_excerpt_sentences_includes_whole_sentences_that_fit():
    s1 = "First sentence of the rationale."
    s2 = "Second sentence of the rationale."
    out = excerpt_sentences(f"{s1} {s2}", max_chars=100)
    assert out == f"{s1} {s2}"


def test_excerpt_sentences_empty_input():
    assert excerpt_sentences("", max_chars=600) == ""
    assert excerpt_sentences("   ", max_chars=600) == ""


def test_excerpt_sentences_does_not_split_on_abbreviations():
    """PR-50 review finding: 'e.g.', 'Dr.', 'U.S.' are not sentence
    boundaries. The preview must continue past the abbreviation rather
    than end the excerpt there."""
    text = ("Some claim e.g. that the evidence overwhelmingly supports the "
            "treatment in every case observed so far and the results are "
            "consistent with the hypothesis. " * 10)
    out = excerpt_sentences(text, max_chars=90)
    assert "e.g." in out
    # the excerpt continues past the abbreviation, it does not stop at it
    assert out.split("e.g.", 1)[1].strip()


def test_excerpt_sentences_etc_terminal_still_splits():
    """PR-50 round-4 finding: a sentence ending in 'etc.' must still be a
    boundary. 'etc.' is conditional (like Inc.), so '..., etc. Choice B...'
    splits at the real sentence end."""
    s1 = "The examples include apples, pears, etc."
    s2 = ("Choice B is the best answer because it stays within the scope "
          "of the passage and does not overstate the evidence.")
    out = excerpt_sentences(f"{s1} {s2} {s2}", max_chars=len(s1) + 20)
    assert out == s1


def test_excerpt_sentences_etc_nonterminal_kept():
    """'etc.' followed by a lowercase continuation stays protected."""
    out = excerpt_sentences("The list includes apples, pears, etc. and other fruit.",
                            max_chars=200)
    assert "etc." in out and "other fruit" in out


def test_excerpt_sentences_still_splits_on_real_periods():
    s1 = "First sentence ends here."
    s2 = "Second sentence starts here."
    out = excerpt_sentences(f"{s1} {s2}", max_chars=100)
    assert out == f"{s1} {s2}"


def test_excerpt_sentences_terminal_abbreviation_still_splits():
    """PR-50 round-2 finding: 'Acme Inc.' at a real sentence end is a
    boundary — protecting it unconditionally hid the boundary and the
    fallback cut the next sentence midstream."""
    s1 = "The company is Acme Inc."
    s2 = ("Choice B is the best answer because it stays within the scope "
          "of the passage and does not overstate the evidence.")
    out = excerpt_sentences(f"{s1} {s2} {s2}", max_chars=len(s1) + 20)
    # the first (complete) sentence fits; the excerpt must stop there,
    # not word-cut into the second sentence
    assert out == s1


def test_excerpt_sentences_terminator_before_closing_punctuation():
    """PR-50 round-2 finding: a sentence ending with quoted or
    parenthesized text ('unexpected.") must still be recognized as a
    boundary even though whitespace follows the quote, not the period."""
    s1 = "The result was \u201cunexpected.\u201d"
    s2 = ("Choice B is the best answer because it stays within the scope "
          "of the passage and does not overstate the evidence.")
    out = excerpt_sentences(f"{s1} {s2} {s2}", max_chars=len(s1) + 20)
    assert out == s1

    paren = "The answer is correct (as shown)."
    out2 = excerpt_sentences(f"{paren} {s2} {s2}", max_chars=len(paren) + 20)
    assert out2 == paren


def test_excerpt_sentences_answer_label_is_not_an_initial():
    """PR-50 round-3 finding: 'The correct answer is A. Choice B...' —
    the lone capital-period answer label is a real boundary, not a name
    initial, so the excerpt must stop at the complete first sentence."""
    s1 = "The correct answer is A."
    s2 = ("Choice B is the best answer because it stays within the scope "
          "of the passage and does not overstate the evidence.")
    out = excerpt_sentences(f"{s1} {s2} {s2}", max_chars=len(s1) + 20)
    assert out == s1


def test_excerpt_sentences_abbreviation_before_uppercase_word():
    """PR-50 round-3 finding: 'Brown vs. Board' — a nonterminal
    abbreviation before an uppercase proper noun must stay protected, not
    split the sentence at the abbreviation."""
    s1 = "Brown vs. Board of Education established the principle."
    s2 = ("The second sentence is long and continues well past the cap "
          "to test the boundary logic.")
    out = excerpt_sentences(f"{s1} {s2} {s2}", max_chars=len(s1) + 20)
    assert out == s1


def test_why_key_works_uses_first_paragraph_only():
    from satprep.training.sessions import _why_key_works
    para1 = "The key works because it matches the passage."
    para2 = "A second paragraph with more official detail."
    out = _why_key_works(f"{para1}\n\n{para2}")
    assert para1 in out
    assert "A second paragraph" not in out


def test_why_key_works_empty_rationale():
    from satprep.training.sessions import _why_key_works
    assert _why_key_works("") == ""


def test_paragraphs_preserve_boundaries_and_drop_blanks():
    from satprep.training.sessions import _paragraphs
    assert _paragraphs("first\n\nsecond\nthird") == ["first", "second", "third"]
    assert _paragraphs("") == []
    assert _paragraphs("single block, no newlines") == ["single block, no newlines"]
