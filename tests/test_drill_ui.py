"""The drill surface: tap count, inline feedback, and the operator/student split.

Issue #26. The engine was fine; the surface cost three interactions and a page
reload per question, revealed nothing until the drill ended, and put the
operator's pages in the student's nav. These tests pin the parts of that which
are behaviour rather than taste — the stylesheet itself is not asserted on
beyond the two things that are load-bearing (dark mode, tap targets).
"""

import pathlib
import re

import pytest

from satprep import server as server_mod
from satprep.analytics import (full_dashboard, next_action, recent_session_scores,
                               session_comparison)
from satprep.db import db_context
from satprep.training.sessions import (answer_feedback, complete_session,
                                       create_session, current_streak,
                                       submit_answer)
from conftest import add_attempt, add_question

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "satprep" / "templates"
STATIC = REPO / "satprep" / "static"


@pytest.fixture()
def live(db, monkeypatch):
    """A corpus big enough to build a drill from, wired into the app."""
    conn, path = db
    for i in range(8):
        add_question(conn, passage=f"passage {i}", stem=f"stem {i}?",
                     choices=[f"q{i}-{letter}" for letter in "abcd"], correct="B",
                     source="bluebook_test", pool="historical")
    conn.commit()
    conn.close()

    def _ctx(db_path=None):
        return db_context(path)

    monkeypatch.setattr(server_mod, "db_context", _ctx)
    return path


# ------------------------------------------------------- two taps, no scroll --

def test_confidence_controls_are_submit_buttons():
    """The old form needed a choice radio, a confidence radio, then a scroll to
    a separate submit: three interactions per question, 36+ for a 12-question
    drill. A submit button carries its own name/value, so confidence and
    submission are one tap - and it is markup, so it holds with JS disabled."""
    markup = (TEMPLATES / "question.html").read_text()

    assert 'type="submit" name="confidence"' in markup
    for value in ("1", "2", "3"):
        assert f'name="confidence" value="{value}"' in markup

    # ...and no separate submit remains to scroll to
    assert markup.count("type=\"submit\"") == 3


def test_the_question_form_still_works_without_javascript():
    """Progressive enhancement, not a rewrite: the form posts to the same
    handler with every field it needs already in the markup."""
    markup = (TEMPLATES / "question.html").read_text()

    assert 'method="post"' in markup
    assert 'action="/answer/{{ sid }}/{{ idx }}"' in markup
    # elapsed_ms is a real field with a default, so a JS-less submit is valid
    # rather than dropping the timing the sampler reads
    assert 'name="elapsed_ms"' in markup and 'value="0"' in markup
    assert 'name="question_id"' in markup
    assert 'name="letter"' in markup


def test_no_page_depends_on_javascript_to_navigate():
    """Every template's controls are links or form submits. A drill on a phone
    with a blocked script must still advance."""
    for template in TEMPLATES.glob("*.html"):
        body = template.read_text()
        assert "onclick=" not in body, f"{template.name} wires behaviour to onclick"
        assert "href=\"javascript:" not in body, f"{template.name} uses a javascript: URL"


def test_the_script_is_local_and_deferred():
    """The box serves this on a LAN and should not depend on the internet
    being up, so no CDN; and one file, so there is no build step."""
    base = (TEMPLATES / "base.html").read_text()
    assert '<script src="/static/app.js" defer></script>' in base
    assert "//" not in re.search(r'<script src="([^"]+)"', base).group(1)
    assert (STATIC / "app.js").exists()
    assert len(list(STATIC.glob("*.js"))) == 1


def test_tap_targets_clear_the_touch_minimum():
    """.55rem of padding is about 26px tall - below the ~44px minimum, which
    on a phone makes every choice a coin toss."""
    css = (STATIC / "style.css").read_text()
    assert "--tap: 2.75rem" in css        # 44px at the default root size
    assert "min-height: var(--tap)" in css


# --------------------------------------------------------- inline feedback --

def test_answering_leads_to_feedback_not_the_next_question(live):
    """Right/wrong used to appear only in /review/{sid}, after the drill. The
    moment right after answering is the one that matters for learning."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="fb")
        sid = sess["plan"]["session_id"]
        qid = sess["questions"][0]["id"]

        response = server_mod.answer(None, sid, 0, question_id=qid, letter="B",
                                     confidence=3, elapsed_ms=1000, conn=conn)

    assert response.headers["location"] == f"/feedback/{sid}/0"


def test_feedback_carries_the_key_and_the_reason(live):
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="fb2")
        sid = sess["plan"]["session_id"]
        qid = sess["questions"][0]["id"]
        submit_answer(conn, sid, qid, "A", 3, 900)   # key is B

        fb = answer_feedback(conn, sid, qid)

    assert fb["correct"] is False
    assert fb["key_letter"] == "B"
    assert fb["key_text"]                    # the key's text, to read in the moment
    assert fb["chosen_letter"] == "A"
    assert fb["chosen_text"]
    assert fb["why_key_works"]               # one line of rationale


def test_feedback_never_reveals_a_key_for_an_unanswered_question(live):
    """/feedback/{sid}/{idx} is a GET, so it is guessable. Rendering it for a
    question that was never answered would hand out a free answer."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="fb3")
        sid = sess["plan"]["session_id"]

        assert answer_feedback(conn, sid, sess["questions"][0]["id"]) is None

        response = server_mod.feedback(None, sid, 0, conn=conn)

    assert response.status_code == 303
    assert response.headers["location"] == f"/question/{sid}/0"


def test_feedback_for_an_unknown_session_does_not_500(live):
    with db_context(live) as conn:
        response = server_mod.feedback(None, "no-such-session", 0, conn=conn)
    assert response.headers["location"] == "/start"


def test_feedback_past_the_last_question_goes_to_results(live):
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="fb4")
        sid = sess["plan"]["session_id"]
        response = server_mod.feedback(None, sid, 99, conn=conn)
    assert response.headers["location"] == f"/results/{sid}"


# ----------------------------------------------------------------- streaks --

def test_streak_counts_consecutive_correct_answers_from_the_end(live):
    """A 4px progress bar was the only signal across a 27-question module."""
    with db_context(live) as conn:
        sess = create_session(conn, "hard_mixed", count=4, seed="streak")
        sid = sess["plan"]["session_id"]
        qs = sess["questions"]

        assert current_streak(conn, sid) == 0

        submit_answer(conn, sid, qs[0]["id"], "B", 3, 100)   # correct
        submit_answer(conn, sid, qs[1]["id"], "B", 3, 100)   # correct
        assert current_streak(conn, sid) == 2

        submit_answer(conn, sid, qs[2]["id"], "A", 3, 100)   # wrong: resets
        assert current_streak(conn, sid) == 0

        submit_answer(conn, sid, qs[3]["id"], "B", 3, 100)
        assert current_streak(conn, sid) == 1


# ------------------------------------------------- results against her own --

def test_results_compare_against_recent_sessions_not_bare_counts(live):
    with db_context(live) as conn:
        first = create_session(conn, "error_clinic", count=2, seed="cmp1")
        sid1 = first["plan"]["session_id"]
        for q in first["questions"]:
            submit_answer(conn, sid1, q["id"], "A", 2, 100)   # all wrong: 0%
        complete_session(conn, sid1)

        second = create_session(conn, "error_clinic", count=2, seed="cmp2")
        sid2 = second["plan"]["session_id"]
        for q in second["questions"]:
            submit_answer(conn, sid2, q["id"], "B", 3, 100)   # all right: 100%
        summary = complete_session(conn, sid2)
        comparison = session_comparison(conn, sid2, summary)

    assert comparison["accuracy"] == 100.0
    assert comparison["baseline"] == 0.0
    assert comparison["delta"] == 100.0
    assert comparison["is_personal_best"] is True
    # its own row must not be in the reference class it is compared against
    assert sid2 not in {p["id"] for p in comparison["previous"]}


def test_a_first_session_reads_as_a_baseline_not_an_improvement(live):
    """delta is None with no prior session, so the screen says "first session"
    rather than reporting an improvement of zero against nothing."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="first")
        sid = sess["plan"]["session_id"]
        for q in sess["questions"]:
            submit_answer(conn, sid, q["id"], "B", 3, 100)
        summary = complete_session(conn, sid)
        comparison = session_comparison(conn, sid, summary)

    assert comparison["accuracy"] == 100.0
    assert comparison["baseline"] is None
    assert comparison["delta"] is None
    assert comparison["is_personal_best"] is False


def test_recent_scores_ignore_the_imported_history(live):
    """`mode='historical'` attempts are the scraped Bluebook backlog, not
    sessions she sat. Counting them would put a wall of 100%s on the screen."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="hist")
        sid = sess["plan"]["session_id"]
        submit_answer(conn, sid, sess["questions"][0]["id"], "B", 3, 100)
        complete_session(conn, sid)

        scores = recent_session_scores(conn)

    assert [s["id"] for s in scores] == [sid]
    assert scores[0]["n"] == 1


def test_an_empty_database_still_answers_what_to_do_next(live):
    """The dashboard leads with next_action, so it has to hold on day zero."""
    with db_context(live) as conn:
        action = next_action(conn)
        assert action["has_history"] is False
        assert action["last_session_at"] is None
        assert recent_session_scores(conn) == []


# ----------------------------------------------- the operator/student split --

def test_the_student_nav_carries_only_her_two_surfaces():
    """Admin and Benchmark in the top-level nav are the operator's tools; the
    benchmark in particular spends protected questions that exist to give one
    honest measurement."""
    nav = (TEMPLATES / "base.html").read_text().split("<nav>", 1)[1].split("</nav>", 1)[0]

    assert 'href="/start"' in nav
    assert 'href="/progress"' in nav
    for operator_only in ('href="/admin"', 'href="/benchmark"',
                          'href="/weaknesses"', 'href="/history"'):
        assert operator_only not in nav, f"{operator_only} is an operator surface"


def test_the_operator_pages_are_still_reachable_from_admin():
    """Out of the nav, not out of the app - the operator still needs them."""
    admin = (TEMPLATES / "admin.html").read_text()
    for page in ("/weaknesses", "/history", "/benchmark"):
        assert f'href="{page}"' in admin


def test_the_dashboard_leads_with_an_action_not_the_corpus_size(live):
    """Corpus counts tell the operator the ingest worked and tell the student
    nothing she can act on. They moved to /admin."""
    dashboard = (TEMPLATES / "dashboard.html").read_text()
    head = dashboard.split("{% block content %}", 1)[1][:900]

    assert "Start drill" in head
    assert "questions in corpus" not in dashboard
    assert "questions active" in (TEMPLATES / "admin.html").read_text()

    with db_context(live) as conn:
        response = server_mod.admin(None, conn=conn)
    assert response.status_code == 200


def test_progress_renders_on_an_empty_database(live):
    with db_context(live) as conn:
        assert server_mod.progress(None, conn=conn).status_code == 200
        assert server_mod.dashboard(None, conn=conn).status_code == 200


def test_weakness_data_reads_as_bars_with_the_table_behind_a_disclosure():
    """Three dense tables on a 390px screen. The comparison is the point, and
    a bar makes it without the student parsing a grid of numbers."""
    progress = (TEMPLATES / "progress.html").read_text()

    assert "bar-fill" in progress and "bar-track" in progress
    assert "<details>" in progress
    # the full tables are still there, one disclosure away
    assert progress.count("<table>") == 2
    for table in progress.split("<table>")[1:]:
        assert "</details>" in table, "a full table escaped its <details>"


# -------------------------------------------------------------- the basics --

def test_weakness_bars_are_scaled_to_the_risk_score_not_pinned_full(live):
    """Regression: risk_score is a 0-100 scale (weakness.score_from returns
    `round(min(100.0, score), 1)`), but the bar width was written as though it
    were a 0-1 fraction, with a `<= 1` guard falling through to a literal 100.
    Every bar therefore rendered full width and the comparison the bars exist
    to make was destroyed. Rendering the real template is the only thing that
    catches it - reading the markup does not.
    """
    with db_context(live) as conn:
        qids = [add_question(conn, stem=f"weak {i}?",
                             tags=("qualifier_strength",) if i % 2 else ("scope_shift",),
                             source="bluebook_test", pool="historical")
                for i in range(12)]
        for i, qid in enumerate(qids):
            add_attempt(conn, qid, correct=(i % 3 == 0))
        conn.commit()

        d = full_dashboard(conn)
        widths = _rendered_bar_widths("progress.html", {"d": d})

    assert d["tags"], "fixture produced no tag rows to chart"
    for t in d["tags"]:
        assert 0 <= t["risk_score"] <= 100
        # the scale really is 0-100, so a fraction-shaped guard is wrong
        assert t["risk_score"] > 1

    risk_widths = widths["risk"]
    assert risk_widths, "no risk bars rendered"
    assert not all(w == 100 for w in risk_widths), "every risk bar is pinned full width"
    for width, tag in zip(risk_widths, d["tags"][:5]):
        assert width == min(tag["risk_score"], 100)


def test_session_accuracy_bars_stay_within_the_track(live):
    """Accuracy is already a percentage; a bar wider than its track would
    overflow the rounded corners rather than clip."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="width")
        sid = sess["plan"]["session_id"]
        for q in sess["questions"]:
            submit_answer(conn, sid, q["id"], "B", 3, 100)
        complete_session(conn, sid)

        d = full_dashboard(conn)
        widths = _rendered_bar_widths("progress.html", {"d": d})

    assert widths["plain"], "no session bars rendered"
    for w in widths["plain"]:
        assert 0 <= w <= 100


def _rendered_bar_widths(template_name: str, context: dict) -> dict:
    """Render a template for real and read the bar widths back out of it."""
    html = server_mod.templates.get_template(template_name).render(**context)
    risk, plain = [], []
    for match in re.finditer(r'class="bar-fill( risk)?" style="width: ([0-9.]+)%', html):
        (risk if match.group(1) else plain).append(float(match.group(2)))
    return {"risk": risk, "plain": plain}


def test_dark_mode_follows_the_system_setting():
    css = (STATIC / "style.css").read_text()
    assert "@media (prefers-color-scheme: dark)" in css
    # the scheme flips custom properties, so it stays one block rather than a
    # second stylesheet
    dark = css.split("@media (prefers-color-scheme: dark)", 1)[1]
    assert "--paper:" in dark and "--ink:" in dark
    assert "color-scheme: dark" in dark
    assert '<meta name="color-scheme" content="light dark">' in (TEMPLATES / "base.html").read_text()


def test_the_serif_fallback_is_gone_from_the_ui_font():
    """`font: 16px/1.55 -apple-system, "Segoe UI", Roboto, serif` rendered the
    whole UI in a serif on any box without one of the three named faces. UI
    chrome is sans; only passages opt into the reading face."""
    css = (STATIC / "style.css").read_text()
    ui = re.search(r"--font-ui:([^;]+);", css).group(1)
    assert "serif" not in ui.replace("sans-serif", "")
    assert "--font-read:" in css
    assert "font-family: var(--font-read)" in css.split(".passage {", 1)[1][:200]


def test_no_new_runtime_dependency_and_no_build_step():
    """One `uv run` on a headless box. A bundler would add a step to
    install.sh and a second thing to keep current."""
    pyproject = (REPO / "pyproject.toml").read_text()
    deps = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert set(re.findall(r'"([a-z0-9\-]+)', deps)) == {
        "beautifulsoup4", "fastapi", "uvicorn", "jinja2", "python-multipart"}

    for marker in ("package.json", "node_modules", "vite.config.js", "webpack.config.js"):
        assert not (REPO / marker).exists(), f"{marker} is a build step"


def test_keyboard_shortcuts_are_wired_without_stealing_typed_input():
    """A-D to choose, 1-3 for confidence. A shortcut that fires while the
    count field has focus would submit the drill instead of typing a digit."""
    js = (STATIC / "app.js").read_text()
    assert "keydown" in js
    assert "textarea" in js and "select" in js      # never steal from a field
    assert "metaKey" in js and "ctrlKey" in js      # nor from a browser shortcut
