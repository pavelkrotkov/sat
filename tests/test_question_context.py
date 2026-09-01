"""The original question shown on review/feedback surfaces (issue #47).

The review route used to pass only chosen/key text, tags, a passage
skeleton, and rationale to the template — never the passage, stem,
complete choice list, figures, or source placement. The student was asked
to interpret the explanation without the question that motivated it.

These tests pin the fix: a shared `_question_context.html` partial renders
the full original question on the drill, immediate feedback, and final
review pages, with selected/key state and source metadata visible, without
revealing the key before an answer is committed.
"""

import re

from satprep.training.sessions import (answer_feedback, create_session,
                                       review_payload, submit_answer)
from conftest import add_question

PASSAGE = ("Marine biologists once assumed the deep-sea anglerfish was rare. "
           "Recent trawls, however, suggest it is abundant below 1,000 meters.")
STEM = "Which choice best describes the author's main claim about the anglerfish?"
CHOICES = {
    "A": "It is much more common than previously believed.",
    "B": "It is found only in shallow coastal waters.",
    "C": "It has no natural predators at depth.",
    "D": "It migrates between oceans seasonally.",
}
KEY = "A"
FIG = "p11/module-1/q7/fig.png"
# Jinja auto-escapes the apostrophe in the stem when rendering.
STEM_ESCAPED = STEM.replace("'", "&#39;")


def _drill(conn, *, images=(), count=2, seed="qctx"):
    sess = create_session(conn, "error_clinic", count=count, seed=seed)
    return sess["plan"]["session_id"], sess["questions"]


def _answer_all(conn, sid, questions, wrong=True):
    for q in questions:
        letter = "B" if wrong else q["choices"][0]["letter"]
        submit_answer(conn, sid, q["id"], letter, 3, 900)


# ------------------------------------------------------------- view model --

def _seed_question(conn, *, images=(), source_test="SAT Practice Test 9",
                   source_question_number="14", module="Module 2"):
    """Insert the fixture question into the `historical` pool so the
    error_clinic sampler (historical only) actually serves it."""
    return add_question(conn, passage=PASSAGE, stem=STEM,
                        choices=list(CHOICES.values()), correct=KEY,
                        images=images, source="bluebook_test",
                        source_test=source_test,
                        source_question_number=source_question_number,
                        module=module)


def test_review_payload_carries_the_full_question(db):
    conn, _ = db
    _seed_question(conn, images=(FIG,))
    conn.commit()
    sid, questions = _drill(conn)
    _answer_all(conn, sid, questions)

    reviews = review_payload(conn, sid)
    assert reviews, "wrong answers should produce review rows"
    ctx = reviews[0]["question"]

    assert ctx["passage"] == PASSAGE
    assert ctx["stem"] == STEM
    assert [c["letter"] for c in ctx["choices"]] == ["A", "B", "C", "D"]
    assert [c["text"] for c in ctx["choices"]] == list(CHOICES.values())
    assert ctx["images"] == [FIG]
    assert ctx["correct_letter"] == KEY
    # source placement is visible for debugging
    assert "SAT Practice Test 9" in ctx["source_label"]
    assert "14" in ctx["source_label"]
    assert "Module 2" in ctx["source_label"]
    # selected/key state is threaded through
    assert ctx["chosen_letter"] == "B"
    assert ctx["key_letter"] == KEY


def test_answer_feedback_carries_the_full_question(db):
    conn, _ = db
    _seed_question(conn, images=(FIG,))
    conn.commit()
    sid, questions = _drill(conn)
    submit_answer(conn, sid, questions[0]["id"], "B", 3, 900)

    fb = answer_feedback(conn, sid, questions[0]["id"])
    ctx = fb["question"]

    assert ctx["passage"] == PASSAGE
    assert ctx["stem"] == STEM
    assert len(ctx["choices"]) == 4
    assert ctx["images"] == [FIG]
    assert ctx["chosen_letter"] == "B"
    assert ctx["key_letter"] == KEY


# ---------------------------------------------------------- templates --

def _render_review(db, *, images=()):
    conn, _ = db
    _seed_question(conn, images=images)
    conn.commit()
    sid, questions = _drill(conn)
    _answer_all(conn, sid, questions)
    return conn, sid


def test_review_page_shows_the_original_question_before_the_rationale(db):
    """Acceptance criterion: a wrong-answer review shows the exact original
    passage/stem, all choices, and visuals BEFORE the rationale."""
    from satprep import server as server_mod

    conn, sid = _render_review(db, images=(FIG,))
    html = server_mod.review(None, sid, conn=conn).body.decode()

    # the full original question is present
    assert PASSAGE in html
    assert STEM_ESCAPED in html
    for text in CHOICES.values():
        assert text in html
    assert f'/figures/{FIG.rsplit("/", 1)[-1]}' in html
    # chosen and key are both marked
    assert "your answer" in html
    assert "key" in html
    # the learning annotations stay below it
    assert html.index(PASSAGE) < html.index("official rationale")
    # source placement is visible for debugging
    assert "SAT Practice Test 9" in html
    assert "source:" in html


def test_feedback_page_marks_both_states_on_a_correct_answer(db):
    """A correct (low-confidence) answer: the chosen letter IS the key, so
    the row must show both 'your answer' and 'key' state without breaking."""
    from satprep import server as server_mod

    conn, _ = db
    _seed_question(conn, images=(FIG,))
    conn.commit()
    sid, questions = _drill(conn)
    # answer correctly (A is the key) but with low confidence
    submit_answer(conn, sid, questions[0]["id"], "A", 1, 900)

    html = server_mod.feedback(None, sid, 0, conn=conn).body.decode()

    assert 'class="choice chosen key"' in html
    assert html.count("your answer") == 1
    assert html.count(">key<") == 1


def test_correct_answer_chosen_label_uses_the_success_color(db):
    """Round-1 Codex P2: on a correct answer the chosen row is also the key,
    but the 'your answer' label kept the wrong-answer red, visually saying a
    correct selection was wrong. The CSS must override it to the accent."""
    import pathlib

    from satprep import server as server_mod

    css = pathlib.Path(server_mod.__file__).parent / "static" / "style.css"
    text = css.read_text()
    # the override exists and targets exactly the chosen+key row
    assert ".choice-list .choice.chosen.key .state:not(.key)" in text
    assert "color: var(--accent)" in text


def test_review_page_marks_chosen_and_key_choices(db):
    conn, sid = _render_review(db)
    html = server_review_body(conn, sid)

    chosen = re.search(r'<li class="choice chosen[^"]*">.*?</li>', html, re.S)
    key = re.search(r'<li class="choice key[^"]*">.*?</li>', html, re.S)
    assert chosen is not None, "the chosen (wrong) choice is not marked"
    assert key is not None, "the key choice is not marked"
    chosen_html, key_html = chosen.group(0), key.group(0)
    assert "B" in chosen_html and "your answer" in chosen_html
    assert "A" in key_html and "key" in key_html


def server_review_body(conn, sid):
    from satprep import server as server_mod

    return server_mod.review(None, sid, conn=conn).body.decode()


def test_feedback_page_shows_the_original_question(db):
    """Acceptance criterion: immediate feedback shows the same question
    context, so the two review surfaces cannot drift."""
    from satprep import server as server_mod

    conn, _ = db
    _seed_question(conn, images=(FIG,))
    conn.commit()
    sid, questions = _drill(conn)
    submit_answer(conn, sid, questions[0]["id"], "B", 3, 900)

    html = server_mod.feedback(None, sid, 0, conn=conn).body.decode()

    assert PASSAGE in html
    assert STEM_ESCAPED in html
    for text in CHOICES.values():
        assert text in html
    assert f'/figures/{FIG.rsplit("/", 1)[-1]}' in html
    assert "your answer" in html
    assert "key" in html
    # the verdict leads, the question comes right after, the key-line below
    assert html.index("Not quite") < html.index(PASSAGE) < html.index("Key — A")


def test_question_page_uses_the_shared_partial_and_stays_submittable(db):
    """The drill must render through the same partial without losing the
    radio inputs the answer form posts."""
    import pathlib

    from satprep import server as server_mod

    qhtml = pathlib.Path(server_mod.__file__).parent / "templates" / "question.html"
    partial = pathlib.Path(server_mod.__file__).parent / "templates" / "_question_context.html"
    markup = qhtml.read_text()
    assert "_question_context.html" in markup
    # the drill passes `selectable` (radios) — asserted in server.py's handler
    import inspect
    src = inspect.getsource(server_mod.question)
    assert '"selectable": True' in src
    # radios preserved in the shared partial: the form still submits a `letter`
    partial_markup = partial.read_text()
    assert 'type="radio" name="letter"' in partial_markup
    assert 'name="letter"' in partial_markup


def test_text_only_question_renders_without_figures(db):
    """A text-only question (no stored visuals) must not render an empty
    figure block or crash."""
    from satprep import server as server_mod

    conn, sid = _render_review(db)
    html = server_mod.review(None, sid, conn=conn).body.decode()

    assert STEM_ESCAPED in html
    assert '<div class="figures">' not in html
    assert 'class="figures"' not in html


def test_question_context_is_escaped(db):
    """Source markup must be safely escaped — a stem or passage with markup
    renders as text, never as injected HTML."""
    from satprep import server as server_mod

    conn, _ = db
    _seed_question(conn)
    conn.execute("UPDATE questions SET passage='<script>alert(1)</script>' WHERE id=?",
                 (conn.execute("SELECT id FROM questions").fetchone()["id"],))
    conn.commit()
    sid, questions = _drill(conn)
    _answer_all(conn, sid, questions)

    html = server_mod.review(None, sid, conn=conn).body.decode()

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
