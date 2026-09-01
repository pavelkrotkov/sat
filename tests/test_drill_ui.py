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
    handler with every field it needs already in the markup. The choices
    now render through the shared question-context partial (issue #47), so
    the radio that carries `letter` lives there."""
    markup = (TEMPLATES / "question.html").read_text()
    partial = (TEMPLATES / "_question_context.html").read_text()

    assert 'method="post"' in markup
    assert 'action="/answer/{{ sid }}/{{ idx }}"' in markup
    # elapsed_ms is a real field with a default, so a JS-less submit is valid
    # rather than dropping the timing the sampler reads
    assert 'name="elapsed_ms"' in markup and 'value="0"' in markup
    assert 'name="question_id"' in markup
    assert 'name="letter"' in partial


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


def test_reopening_an_old_session_keeps_its_own_baseline(live):
    """Issue #28: the student can reopen an old results page. Its historical
    comparison must not drift — a session that was her first (no prior baseline)
    must still read as a baseline when reopened after a later drill, because
    session_comparison only counts sessions that finished *before* it."""
    with db_context(live) as conn:
        old = create_session(conn, "error_clinic", count=2, seed="reopen-old")
        sid_old = old["plan"]["session_id"]
        for q in old["questions"]:
            submit_answer(conn, sid_old, q["id"], "B", 3, 100)
        summary_old = complete_session(conn, sid_old)

        # a later drill; must not creep into the old session's baseline
        new = create_session(conn, "error_clinic", count=2, seed="reopen-new")
        sid_new = new["plan"]["session_id"]
        for q in new["questions"]:
            submit_answer(conn, sid_new, q["id"], "B", 3, 100)
        complete_session(conn, sid_new)

        # reopen the old session's results
        response = server_mod.results(None, sid_old, conn=conn)
        comparison = session_comparison(conn, sid_old, summary_old)

    assert response.status_code == 200
    # its own score is preserved, and it still reads as a first-session baseline
    assert comparison["accuracy"] == 100.0
    assert comparison["baseline"] is None
    assert comparison["is_personal_best"] is False
    # the later session is not in its reference class
    assert sid_new not in {p["id"] for p in comparison["previous"]}


def test_reopened_results_page_carries_its_own_mistake_review(live):
    """From a reopened /results/{sid} the existing mistake-review link must
    still point at that same session's /review/{sid}."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="reopen-link")
        sid = sess["plan"]["session_id"]
        for q in sess["questions"]:
            submit_answer(conn, sid, q["id"], "A", 3, 100)   # wrong on purpose
        complete_session(conn, sid)

        response = server_mod.results(None, sid, conn=conn)

    html = response.body.decode()
    assert f'href="/review/{sid}"' in html
    assert "Review mistakes" in html


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


def test_admin_stays_reachable_without_typing_a_url(live):
    """Round-1 review: moving the operator pages behind /admin while removing
    every link to /admin left the whole operator surface reachable only by
    knowing the URL. A footer link is discoverable without being a third tab
    beside Drill and Progress."""
    base = (TEMPLATES / "base.html").read_text()
    footer = base.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert 'href="/admin"' in footer

    # and it renders on the student's own pages, not just in the source
    with db_context(live) as conn:
        html = server_mod.templates.get_template("progress.html").render(
            d=full_dashboard(conn))
    assert 'href="/admin"' in html


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


def test_session_rows_link_to_their_results_page(live):
    """The score rows were presentation divs; the only way to reopen a result
    was the opaque /results/{id} URL. Every rendered row must carry a link to
    its own results page, with accessible text that says which session."""
    with db_context(live) as conn:
        first = create_session(conn, "error_clinic", count=2, seed="row1")
        sid1 = first["plan"]["session_id"]
        for q in first["questions"]:
            submit_answer(conn, sid1, q["id"], "B", 3, 100)
        complete_session(conn, sid1)

        second = create_session(conn, "error_clinic", count=2, seed="row2")
        sid2 = second["plan"]["session_id"]
        for q in second["questions"]:
            submit_answer(conn, sid2, q["id"], "A", 3, 100)
        complete_session(conn, sid2)

        d = full_dashboard(conn)
        # /progress enriches the dashboard with the unbounded finished list;
        # mirror that here since we render the template rather than hit the route.
        d["all_sessions"] = recent_session_scores(conn, limit=None)

    dashboard = server_mod.templates.get_template("dashboard.html").render(d=d)
    progress = server_mod.templates.get_template("progress.html").render(d=d)

    for html in (dashboard, progress):
        assert f'href="/results/{sid1}"' in html, "a session row is not a link"
        assert f'href="/results/{sid2}"' in html
        # the link text names the session rather than being a bare chevron
        for sid in (sid1, sid2):
            link = re.search(rf'<a [^>]*href="/results/{sid}"[^>]*>(.*?)</a>',
                             html, re.S)
            assert link, f"no <a> for {sid}"
            assert link.group(1).strip(), f"link for {sid} has no accessible text"
        # both sessions are same-day (same created_at date) and share the mode,
        # so a date-only aria-label would name them identically. The two
        # accessible names must differ so screen-reader link navigation can
        # tell them apart.
        labels = re.findall(r'<a [^>]*href="/results/(?:'
                            + re.escape(sid1) + "|" + re.escape(sid2) + ')"'
                            + r'[^>]*aria-label="([^"]*)"', html)
        assert len(labels) == 2, "each same-day session needs its own aria-label"
        assert len(set(labels)) == 2, "same-day sessions share an accessible name"


def test_progress_lists_every_completed_session_not_just_eight(live):
    """The dashboard slice is eight; Progress is where a student browses the
    rest. With ten finished sessions, /progress must link all ten."""
    with db_context(live) as conn:
        sids = []
        for i in range(10):
            sess = create_session(conn, "error_clinic", count=1, seed=f"all{i}")
            sid = sess["plan"]["session_id"]
            submit_answer(conn, sid, sess["questions"][0]["id"], "B", 3, 100)
            complete_session(conn, sid)
            sids.append(sid)

        response = server_mod.progress(None, conn=conn)

    html = response.body.decode()
    assert response.status_code == 200
    for sid in sids:
        assert f'href="/results/{sid}"' in html, f"session {sid} not browsable on /progress"


def test_progress_empty_state_when_no_completed_sessions(live):
    """With nothing finished the page still renders and says so, rather than
    showing a header with no rows underneath."""
    with db_context(live) as conn:
        response = server_mod.progress(None, conn=conn)

    html = response.body.decode()
    assert response.status_code == 200
    assert 'href="/results/' not in html
    assert "No completed sessions" in html


def test_session_row_links_clear_the_tap_target_minimum():
    """A 30px text line is easy to miss on a 390px-wide phone; the linked row
    needs the same touch floor as the rest of the student surface."""
    css = (STATIC / "style.css").read_text()
    # the session link itself carries the tap floor
    assert re.search(r"\.bar-link\s*\{[^}]*min-height:\s*var\(--tap\)", css), \
        ".bar-link lacks a min-height tap target"


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
        d["all_sessions"] = recent_session_scores(conn, limit=None)
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


# ------------------------------------------------ round-1 review findings --

def test_the_last_answer_completes_the_session(live):
    """Review finding: routing /answer to a feedback screen left completion
    hanging off the optional "See results" tap. A student who closes the tab
    after the final verdict had a fully answered session stuck at 'open',
    permanently missing from analytics with a stale weakness cache - where the
    old redirect chain reached /results on its own."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="last")
        sid = sess["plan"]["session_id"]
        qs = sess["questions"]

        for idx, q in enumerate(qs):
            server_mod.answer(None, sid, idx, question_id=q["id"], letter="B",
                              confidence=3, elapsed_ms=100, conn=conn)
            server_mod.feedback(None, sid, idx, conn=conn)

        status = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        assert status["status"] == "completed", "session left open after the last answer"
        # ...and it is therefore visible to the analytics she is shown
        assert sid in {s["id"] for s in recent_session_scores(conn)}


def test_completing_from_feedback_twice_is_harmless(live):
    """The student can still tap through to /results, which completes again.
    Double completion must not double-count anything."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="twice")
        sid = sess["plan"]["session_id"]
        for idx, q in enumerate(sess["questions"]):
            submit_answer(conn, sid, q["id"], "B", 3, 100)

        server_mod.feedback(None, sid, 1, conn=conn)     # completes
        first = recent_session_scores(conn)
        server_mod.results(None, sid, conn=conn)         # completes again
        second = recent_session_scores(conn)

    assert first == second


def test_the_comparison_baseline_does_not_change_retroactively(live):
    """Review finding: an old results URL fetched the globally newest sessions
    and excluded only its own id, so a first session stopped reading as the
    baseline once a later drill existed. The reference set is now the sessions
    that actually preceded the one being viewed."""
    with db_context(live) as conn:
        first = create_session(conn, "error_clinic", count=2, seed="retro1")
        sid1 = first["plan"]["session_id"]
        for q in first["questions"]:
            submit_answer(conn, sid1, q["id"], "B", 3, 100)
        summary1 = complete_session(conn, sid1)

        at_the_time = session_comparison(conn, sid1, summary1)
        assert at_the_time["baseline"] is None
        assert at_the_time["delta"] is None

        later = create_session(conn, "error_clinic", count=2, seed="retro2")
        sid2 = later["plan"]["session_id"]
        for q in later["questions"]:
            submit_answer(conn, sid2, q["id"], "A", 3, 100)
        complete_session(conn, sid2)

        revisited = session_comparison(conn, sid1, summary1)

    assert revisited["baseline"] is None, "an earlier session gained a later baseline"
    assert revisited["delta"] is None
    assert revisited == at_the_time

    # the later session does see the earlier one
    with db_context(live) as conn:
        summary2 = complete_session(conn, sid2)
        forward = session_comparison(conn, sid2, summary2)
    assert forward["baseline"] == 100.0


def test_sessions_in_the_same_second_still_order_deterministically(live):
    """clock.utc_now is second-resolution, so drills finishing back to back tie
    on the timestamp; a plain `<` would drop the earlier ones out of the
    reference set entirely. The tie-break is the last attempt's autoincrement
    id, which is the real order the work finished in."""
    with db_context(live) as conn:
        ids = []
        for n in range(3):
            sess = create_session(conn, "error_clinic", count=2, seed=f"tie{n}")
            sid = sess["plan"]["session_id"]
            for q in sess["questions"]:
                submit_answer(conn, sid, q["id"], "B", 3, 100)
            complete_session(conn, sid)
            ids.append(sid)

        stamps = {r["attempted_at"] for r in
                  conn.execute("SELECT attempted_at FROM attempts").fetchall()}
        anchor = conn.execute(
            """SELECT MAX(a.attempted_at) AS finished_at, MAX(a.id) AS last_attempt_id
               FROM attempts a WHERE a.session_id = ? AND a.mode != 'historical'""",
            (ids[-1],),
        ).fetchone()
        previous = recent_session_scores(
            conn, exclude=ids[-1],
            before=(anchor["finished_at"], anchor["last_attempt_id"]))

    # the fixture is only meaningful if the timestamps really did collide
    if len(stamps) == 1:
        assert {p["id"] for p in previous} == set(ids[:-1]), \
            "same-second predecessors were dropped from the reference set"


def test_answering_out_of_order_does_not_lock_the_student_out(live):
    """Second-order bug in my own round-1 fix for the completion finding.

    /question and /feedback are guessable GETs, so a drill can be answered out
    of order. Completing on `is_last` alone closed the session the moment the
    LAST question was answered - even with earlier ones outstanding - and
    `submit_answer` refuses a session that is not open, so the student was
    locked out of the rest of her own drill. Completion has to mean "every
    question answered", which is a count, not an index.
    """
    with db_context(live) as conn:
        sess = create_session(conn, "hard_mixed", count=4, seed="ooo")
        sid = sess["plan"]["session_id"]
        qs = sess["questions"]

        # jump straight to the final question
        last = len(qs) - 1
        server_mod.answer(None, sid, last, question_id=qs[last]["id"], letter="B",
                          confidence=3, elapsed_ms=100, conn=conn)
        server_mod.feedback(None, sid, last, conn=conn)

        status = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        assert status["status"] == "open", "session closed with questions outstanding"

        # the rest of the drill is still answerable
        for idx in range(last):
            server_mod.answer(None, sid, idx, question_id=qs[idx]["id"], letter="B",
                              confidence=3, elapsed_ms=100, conn=conn)
            server_mod.feedback(None, sid, idx, conn=conn)

        status = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
        assert status["status"] == "completed", "session never completed"


# ------------------------------------------------ round-2 review findings --

def test_a_benchmark_never_shows_feedback_between_questions(live):
    """Review finding (P1): the fresh benchmark is the one honest measurement
    in the system - its questions are protected, and answering one marks it
    seen irreversibly. Revealing the key and rationale after each answer
    teaches during the measurement, so a later item can benefit from
    instruction delivered mid-benchmark and the baseline can never be retaken.
    """
    with db_context(live) as conn:
        for i in range(10):
            add_question(conn, stem=f"protected {i}?", source="cb",
                         pool="protected_benchmark", correct="B")
        conn.commit()

        sess = create_session(conn, "fresh_benchmark", count=4)
        sid = sess["plan"]["session_id"]
        qs = sess["questions"]
        assert len(qs) >= 2, "need a multi-question benchmark to prove the leak"

        # answering does not route to a verdict
        response = server_mod.answer(None, sid, 0, question_id=qs[0]["id"],
                                     letter="A", confidence=3, elapsed_ms=100,
                                     conn=conn)
        assert response.headers["location"] == f"/question/{sid}/1"

        # ...and the feedback URL is a plain GET, so it needs the same guard
        direct = server_mod.feedback(None, sid, 0, conn=conn)
        assert direct.status_code == 303, "benchmark feedback rendered a key"
        assert direct.headers["location"] == f"/question/{sid}/1"


def test_a_training_drill_still_shows_feedback(live):
    """The benchmark guard must not cost the feature everywhere else."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=2, seed="train")
        sid = sess["plan"]["session_id"]
        response = server_mod.answer(None, sid, 0,
                                     question_id=sess["questions"][0]["id"],
                                     letter="B", confidence=3, elapsed_ms=100,
                                     conn=conn)
    assert response.headers["location"] == f"/feedback/{sid}/0"


def test_a_benchmark_still_reaches_its_results(live):
    """Skipping /feedback must not strand the session: /question past the end
    still redirects to /results, which completes it."""
    with db_context(live) as conn:
        for i in range(10):
            add_question(conn, stem=f"pb-done {i}?", source="cb",
                         pool="protected_benchmark", correct="B")
        conn.commit()

        sess = create_session(conn, "fresh_benchmark", count=3)
        sid = sess["plan"]["session_id"]
        qs = sess["questions"]
        for idx, q in enumerate(qs):
            server_mod.answer(None, sid, idx, question_id=q["id"], letter="B",
                              confidence=3, elapsed_ms=100, conn=conn)

        past_end = server_mod.question(None, sid, len(qs), conn=conn)
        assert past_end.headers["location"] == f"/results/{sid}"
        server_mod.results(None, sid, conn=conn)
        status = conn.execute("SELECT status FROM sessions WHERE id=?", (sid,)).fetchone()
    assert status["status"] == "completed"


def test_progress_reflects_drills_not_only_the_imported_history(live):
    """Review finding: /progress was built from skill_accuracy/tag_accuracy,
    which filter to `a.mode='historical'` - they describe the scraped Bluebook
    backlog. Her own drills moved the weakness score while the wrong/seen
    counts beside it never changed, and a profile built purely from in-app
    answers rendered as "not enough data yet"."""
    with db_context(live) as conn:
        # tag every question in the corpus, so whatever the sampler picks
        # carries the tag - otherwise this asserts on the sampler's choices
        # rather than on the profile the page reads
        for (qid,) in conn.execute("SELECT id FROM questions").fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at)"
                " VALUES (?,'scope_shift','rule','2026-01-01')", (qid,))
        conn.commit()

        sess = create_session(conn, "hard_mixed", count=6, seed="prac")
        sid = sess["plan"]["session_id"]
        for q in sess["questions"]:
            submit_answer(conn, sid, q["id"], "A", 3, 100)     # all wrong
        complete_session(conn, sid)

        d = full_dashboard(conn)

        # the historical-only view is empty: there are no historical attempts
        assert d["tags"] == []
        # ...but the student-facing view is not
        assert d["practice_tags"], "Progress shows nothing despite a full drill"
        names = {t["name"] for t in d["practice_tags"]}
        assert "scope_shift" in names

        entry = next(t for t in d["practice_tags"] if t["name"] == "scope_shift")
        assert entry["seen"] > 0, "wrong/seen never moves with in-app answers"
        assert entry["wrong"] > 0

        # and it renders, rather than claiming there is nothing to show
        html = server_mod.templates.get_template("progress.html").render(d=d)
    # scoped to the reasoning-pattern section: these fixture questions carry no
    # official_skill, so the skills section is legitimately empty here
    tags_section = html.split("Weakest reasoning patterns", 1)[1].split("</section>", 1)[0]
    assert "Not enough answers yet" not in tags_section
    assert "scope_shift" in tags_section


def test_a_comparison_is_frozen_by_completion_not_creation(live):
    """Review finding: `status` was evaluated now while the ordering key was
    creation time, so an older session left open and finished later slid into
    a newer session's baseline after that newer results page had already been
    shown. Ordering on when the work actually finished closes that."""
    with db_context(live) as conn:
        stale = create_session(conn, "error_clinic", count=2, seed="stale")
        stale_sid = stale["plan"]["session_id"]          # created first, left open

        newer = create_session(conn, "error_clinic", count=2, seed="newer")
        newer_sid = newer["plan"]["session_id"]
        for q in newer["questions"]:
            submit_answer(conn, newer_sid, q["id"], "B", 3, 100)
        newer_summary = complete_session(conn, newer_sid)

        before = session_comparison(conn, newer_sid, newer_summary)
        assert before["baseline"] is None, "nothing had finished before it"

        # the older session is only now finished
        for q in stale["questions"]:
            submit_answer(conn, stale_sid, q["id"], "A", 3, 100)
        complete_session(conn, stale_sid)

        after = session_comparison(conn, newer_sid, newer_summary)

    assert after["baseline"] is None, (
        "a session finished later was backdated into an earlier baseline")
    assert after == before


def test_choice_radios_keep_their_intrinsic_size(live):
    """Review finding: the generic `form input` rule is display:block,
    width:100%, min-height:44px. A choice radio inheriting it swallows the
    whole flex row and pushes the letter and answer text out of view - worst
    on the phone layout this PR exists for."""
    css = (STATIC / "style.css").read_text()

    # the generic rule no longer matches a radio or a checkbox at all
    generic = re.search(r"form select,\s*\n(form input[^{]*)\{", css)
    assert generic, "the generic form input rule moved; re-check this guard"
    assert "[type=radio]" in generic.group(1)
    assert "[type=checkbox]" in generic.group(1)

    # ...and the choice radio restores intrinsic sizing explicitly anyway
    choice_rule = css.split(".choice input {", 1)[1].split("}", 1)[0]
    assert "width: auto" in choice_rule
    assert "min-height: 0" in choice_rule


# -------------------------------------------------- full rationale renders -- #
# Issue #48: the official rationale is complete in the database but was
# clipped to 1200 chars mid-sentence in the review template. The full text
# must render, paragraph boundaries preserved.

def _review_html_with_rationale(live, rationale: str) -> str:
    """Answer one question wrong, set its rationale, render /review/{sid}."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=1, seed="rat")
        sid = sess["plan"]["session_id"]
        qid = sess["questions"][0]["id"]
        conn.execute("UPDATE questions SET rationale=? WHERE id=?",
                     (rationale, qid))
        conn.commit()
        submit_answer(conn, sid, qid, "A", 3, 100)   # key is B -> wrong -> review
        complete_session(conn, sid)
        response = server_mod.review(None, sid, conn=conn)
    return response.body.decode()


def test_review_renders_a_rationale_longer_than_1200_chars_in_full(live):
    """Regression for issue #48: the final sentence of a >1200-char
    rationale must appear in the review response."""
    tail = "This final sentence must appear in the review page."
    rationale = ("The best answer is B because the passage supports it. "
                 "The passage states this directly in the second paragraph. " * 30) + tail
    assert len(rationale) > 1200

    html = _review_html_with_rationale(live, rationale)

    assert tail in html
    # the whole text is present, not just a head slice
    assert rationale[:-1] in html or rationale in html


def test_review_renders_multi_paragraph_rationale_with_boundaries(live):
    para1 = "Choice A is wrong because it overstates the evidence."
    para2 = "Choice B is correct because it matches the passage exactly."
    rationale = f"{para1}\n\n{para2}"

    html = _review_html_with_rationale(live, rationale)

    # each paragraph is its own block inside the official-rationale details
    assert f"<p>{para1}</p>" in html
    assert f"<p>{para2}</p>" in html
    # neither paragraph is character-sliced
    assert "[:1200]" not in html


def test_review_compact_summary_is_a_labeled_excerpt(live):
    """The 'why the key works' preview is a sentence-boundary excerpt of the
    official text, and is labeled as such so the distinction stays clear."""
    para1 = ("Choice B is the best answer because it most logically completes "
             "the discussion. " * 40)      # > 600 chars in one paragraph
    para2 = "The other choices are incorrect for unrelated reasons."
    html = _review_html_with_rationale(live, f"{para1}\n\n{para2}")

    assert "(excerpt)" in html
    assert "why the key works" in html


def test_review_renders_null_rationale_without_crashing(live):
    """PR-50 round-6 finding: a NULL rationale (column is nullable) must
    not 500 the review page."""
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=1, seed="rat-null")
        sid = sess["plan"]["session_id"]
        qid = sess["questions"][0]["id"]
        conn.execute("UPDATE questions SET rationale=NULL WHERE id=?", (qid,))
        conn.commit()
        submit_answer(conn, sid, qid, "A", 3, 100)   # wrong -> in review
        complete_session(conn, sid)
        response = server_mod.review(None, sid, conn=conn)

    assert response.status_code == 200
    assert "no official rationale stored" in response.body.decode()


def test_feedback_excerpt_never_cuts_mid_sentence(live):
    """The per-question feedback surface shows the same compact preview; it
    must end at a sentence boundary, never mid-sentence."""
    s1 = "Choice B is best because it stays within the passage's scope."
    s2 = "The remaining choices introduce unsupported claims."
    # many sentences: the 600-char cap must stop at a boundary, not mid-sentence
    rationale = f"{s1} {s2} " * 40 + s2
    with db_context(live) as conn:
        sess = create_session(conn, "error_clinic", count=1, seed="fb-rat")
        sid = sess["plan"]["session_id"]
        qid = sess["questions"][0]["id"]
        conn.execute("UPDATE questions SET rationale=? WHERE id=?",
                     (rationale, qid))
        conn.commit()
        submit_answer(conn, sid, qid, "A", 3, 900)
        fb = answer_feedback(conn, sid, qid)

    assert fb is not None
    assert fb["why_key_works"].endswith(("scope.", "claims."))
    # the last sentence is NOT partially included (no mid-sentence cut)
    assert len(fb["why_key_works"]) <= 600
