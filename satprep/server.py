"""Local web UI (FastAPI + Jinja2). Same DB and selection logic as the CLI.

Screens: dashboard, start drill, question, results, review, weaknesses,
history, fresh benchmark, admin (tag inspection/correction + selection
traces).
"""

import json

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from . import reports as reports_mod
from .analytics import corpus_summary, full_dashboard, recent_session_scores, session_comparison
from .config import REASONING_TAGS
from .corpus.questions import load as load_question
from .corpus.tags import all_tags_with_origin, set_manual, suppress
from .db import db_context
from .training.sessions import (
    answer_feedback,
    complete_session,
    create_session,
    current_streak,
    review_payload,
    submit_answer,
)
from .training.weakness import cached_profile, compute_weakness

app = FastAPI(title="satprep", docs_url=None, redoc_url=None)
# PR-43 review: guard the static mount so the module imports cleanly
# in CI environments where artifacts/ has not been created yet. The
# route is silently absent in that case; the server still serves
# every other endpoint. Starlette's StaticFiles refuses to mount a
# non-existent directory, so we create the directory on the fly if
# it is missing - the figures route is read-only and an empty
# directory is harmless.
_figures_dir = config.REPO_ROOT / "artifacts" / "images"
_figures_dir.mkdir(parents=True, exist_ok=True)
app.mount(
    "/static", StaticFiles(directory=str(config.REPO_ROOT / "satprep" / "static")), name="static"
)
app.mount("/figures", StaticFiles(directory=str(_figures_dir)), name="figures")
templates = Jinja2Templates(directory=str(config.REPO_ROOT / "satprep" / "templates"))
templates.env.filters["basename"] = lambda p: str(p).rsplit("/", 1)[-1]


def get_conn():
    """One connection per request, rolled back if the handler raises.

    A yield dependency's teardown runs *after* the response has been sent, so
    the commit here is a backstop, not the durability guarantee: a handler
    that writes calls `_commit(conn)` before returning, or the client could
    be told the write succeeded and then follow a redirect to a request that
    cannot see it.
    """
    with db_context() as conn:
        yield conn


def _commit(conn) -> None:
    """Make a handler's writes durable before its response leaves."""
    conn.commit()


Conn = Depends(get_conn)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Conn):
    d = full_dashboard(conn)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "d": d,
        },
    )


@app.get("/start", response_class=HTMLResponse)
def start_drill(request: Request, conn=Conn):
    focus = request.query_params.get("focus") or ""
    weak_tags = list(cached_profile(conn, "tag"))[:12]
    counts = dict(
        conn.execute("SELECT pool, COUNT(*) FROM questions WHERE active=1 GROUP BY pool").fetchall()
    )
    return templates.TemplateResponse(
        request,
        "start.html",
        {
            "weak_tags": weak_tags,
            "reasoning_tags": sorted(REASONING_TAGS),
            "pool_counts": counts,
            "focus": focus,
        },
    )


@app.post("/begin")
def begin(
    request: Request,
    mode: str = Form(...),
    count: int = Form(0),
    focus_tag: str = Form(""),
    conn=Conn,
):
    sess = create_session(conn, mode=mode, count=count or None, focus_tag=focus_tag or None)
    sid = sess["plan"]["session_id"]
    _commit(conn)
    return RedirectResponse(f"/question/{sid}/0", status_code=303)


@app.get("/question/{sid}/{idx}", response_class=HTMLResponse)
def question(request: Request, sid: str, idx: int, conn=Conn):
    plan_row = conn.execute("SELECT plan_json FROM sessions WHERE id=?", (sid,)).fetchone()
    if not plan_row:
        return RedirectResponse("/start", status_code=303)
    items = json.loads(plan_row["plan_json"])
    if idx >= len(items):
        return RedirectResponse(f"/results/{sid}", status_code=303)
    q = load_question(conn, items[idx]["question_id"])
    if q is None:
        return RedirectResponse(f"/results/{sid}", status_code=303)
    return templates.TemplateResponse(
        request,
        "question.html",
        {
            "q": q,
            "sid": sid,
            "idx": idx,
            "total": len(items),
            "streak": current_streak(conn, sid),
            # shared partial input (issue #47): plain dict, no template
            # comprehensions — Jinja expressions cannot build lists inline
            "ctx": {
                "passage": q.passage,
                "stem": q.stem,
                "choices": [
                    {"letter": c.letter, "text": c.text, "is_correct": c.is_correct}
                    for c in q.choices
                ],
                "images": list(q.images),
                "visuals": [dict(v) for v in q.visuals],
                "chosen_letter": "",
                "key_letter": "",
                "selectable": True,
                "source_label": "",
            },
        },
    )


@app.post("/answer/{sid}/{idx}")
def answer(
    request: Request,
    sid: str,
    idx: int,
    question_id: int = Form(...),
    letter: str = Form(...),
    confidence: int = Form(...),
    elapsed_ms: int = Form(0),
    conn=Conn,
):
    submit_answer(conn, sid, question_id, letter, confidence, elapsed_ms)
    _commit(conn)
    if _is_measurement(conn, sid):
        # A benchmark is a measurement, not a lesson. Teaching between its
        # questions lets a later item benefit from instruction delivered
        # mid-measurement, and each protected question is irreversibly marked
        # seen on submission - so the baseline cannot be taken again.
        return RedirectResponse(f"/question/{sid}/{idx + 1}", status_code=303)
    # Straight to the feedback screen: the moment right after committing to a
    # choice is the one where the key and the reason land.
    return RedirectResponse(f"/feedback/{sid}/{idx}", status_code=303)


def _is_measurement(conn, sid: str) -> bool:
    """True for sessions whose value depends on not being taught mid-session."""
    row = conn.execute("SELECT mode FROM sessions WHERE id=?", (sid,)).fetchone()
    return bool(row) and row["mode"] == "fresh_benchmark"


@app.get("/feedback/{sid}/{idx}", response_class=HTMLResponse)
def feedback(request: Request, sid: str, idx: int, conn=Conn):
    plan_row = conn.execute("SELECT plan_json FROM sessions WHERE id=?", (sid,)).fetchone()
    if not plan_row:
        return RedirectResponse("/start", status_code=303)
    items = json.loads(plan_row["plan_json"])
    if idx >= len(items):
        return RedirectResponse(f"/results/{sid}", status_code=303)
    payload = answer_feedback(conn, sid, items[idx]["question_id"])
    if payload is None:
        # never answered: revealing the key here would hand out a free answer
        return RedirectResponse(f"/question/{sid}/{idx}", status_code=303)
    if _is_measurement(conn, sid):
        # /answer never sends a benchmark here, but this is a plain GET and
        # therefore guessable; the redirect above would be a fig leaf without
        # the same guard on the route that actually renders the key.
        return RedirectResponse(f"/question/{sid}/{idx + 1}", status_code=303)
    is_last = idx + 1 >= len(items)
    # Completion is "every question answered", not "the last index was
    # reached". /question and /feedback are guessable GETs, so a drill can be
    # answered out of order; keying off `is_last` alone would close the
    # session while earlier questions were still unanswered, and
    # `submit_answer` refuses a session that is not open — locking the student
    # out of her own drill. Counting attempts is the condition that actually
    # means the drill is over.
    answered = conn.execute(
        "SELECT COUNT(DISTINCT question_id) FROM attempts WHERE session_id=?", (sid,)
    ).fetchone()[0]
    if answered >= len(items):
        # The last verdict has been delivered, so the drill is over whether or
        # not she taps "See results". Leaving completion to that click means a
        # closed tab drops a fully answered session out of the analytics and
        # never refreshes the weakness cache; the old redirect chain reached
        # /results on its own. complete_session is idempotent.
        complete_session(conn, sid)
        _commit(conn)
    return templates.TemplateResponse(
        request,
        "feedback.html",
        {
            "fb": payload,
            "sid": sid,
            "idx": idx,
            "total": len(items),
            "is_last": is_last,
            "next_url": (f"/results/{sid}" if is_last else f"/question/{sid}/{idx + 1}"),
        },
    )


@app.get("/results/{sid}", response_class=HTMLResponse)
def results(request: Request, sid: str, conn=Conn):
    summary = complete_session(conn, sid)  # idempotent-ish; refreshes weakness cache
    comparison = session_comparison(conn, sid, summary)
    _commit(conn)
    return templates.TemplateResponse(
        request,
        "results.html",
        {
            "summary": summary,
            "sid": sid,
            "comparison": comparison,
        },
    )


@app.get("/review/{sid}", response_class=HTMLResponse)
def review(request: Request, sid: str, conn=Conn):
    rows = review_payload(conn, sid)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "reviews": rows,
            "sid": sid,
        },
    )


@app.get("/progress", response_class=HTMLResponse)
def progress(request: Request, conn=Conn):
    """The student-facing half of the old /weaknesses and /history pages.

    Weakness data is comparative, so it reads as bars; the full tables stay
    one `<details>` away rather than filling a 390px screen. `all_sessions`
    is the unbounded finished-session list: the dashboard keeps its eight-item
    slice, Progress is where the rest can be browsed and reopened.
    """
    d = full_dashboard(conn)
    d["all_sessions"] = recent_session_scores(conn, limit=None)
    return templates.TemplateResponse(
        request,
        "progress.html",
        {
            "d": d,
        },
    )


@app.get("/weaknesses", response_class=HTMLResponse)
def weaknesses(request: Request, conn=Conn):
    skills = cached_profile(conn, "skill")
    tags = cached_profile(conn, "tag")

    def rows(profile):
        return [
            (entity, p["score"], {k: v for k, v in p.items() if k != "score"})
            for entity, p in profile.items()
        ]

    return templates.TemplateResponse(
        request,
        "weaknesses.html",
        {
            "skills": rows(skills),
            "tags": rows(tags),
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, conn=Conn):
    sessions = conn.execute(
        """SELECT s.id, s.mode, s.created_at, s.status,
                  COUNT(a.id) AS answered,
                  COALESCE(SUM(a.correct),0) AS correct
           FROM sessions s LEFT JOIN attempts a ON a.session_id=s.id AND a.mode!='historical'
           GROUP BY s.id ORDER BY s.created_at DESC LIMIT 50"""
    ).fetchall()
    attempts = conn.execute(
        """SELECT a.attempted_at, a.mode, a.chosen_letter, q.correct_letter,
                  a.confidence, a.correct, q.official_skill
           FROM attempts a JOIN questions q ON q.id=a.question_id
           ORDER BY a.id DESC LIMIT 100"""
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "sessions": sessions,
            "attempts": attempts,
        },
    )


@app.get("/benchmark", response_class=HTMLResponse)
def benchmark_start(request: Request, conn=Conn):
    remaining = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE pool='protected_benchmark' AND seen_benchmark=0"
    ).fetchone()[0]
    return templates.TemplateResponse(
        request,
        "benchmark.html",
        {
            "remaining": remaining,
        },
    )


@app.post("/benchmark/begin")
def benchmark_begin(request: Request, count: int = Form(8), conn=Conn):
    sess = create_session(conn, mode="fresh_benchmark", count=min(count, 25))
    sid = sess["plan"]["session_id"]
    if not sess["questions"]:
        return RedirectResponse("/benchmark", status_code=303)
    _commit(conn)
    return RedirectResponse(f"/question/{sid}/0", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, conn=Conn):
    recent_sessions = conn.execute(
        "SELECT id, mode, created_at, seed, algo_version FROM sessions ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "sessions": recent_sessions,
            "corpus": corpus_summary(conn),
        },
    )


@app.get("/admin/why/{sid}", response_class=HTMLResponse)
def admin_why(request: Request, sid: str, conn=Conn):
    plan_row = conn.execute(
        "SELECT plan_json, mode, seed FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    if not plan_row:
        return RedirectResponse("/admin", status_code=303)
    items = json.loads(plan_row["plan_json"])
    enriched = []
    for it in items:
        q = conn.execute(
            "SELECT official_skill, source_test, source_question_number, pool, difficulty FROM questions WHERE id=?",
            (it["question_id"],),
        ).fetchone()
        enriched.append({**it, "meta": dict(q) if q else {}})
    return templates.TemplateResponse(
        request,
        "why.html",
        {
            "items": enriched,
            "sid": sid,
            "mode": plan_row["mode"],
            "seed": plan_row["seed"],
        },
    )


@app.get("/admin/tags/{qid}", response_class=HTMLResponse)
def admin_tags(request: Request, qid: int, conn=Conn):
    q = load_question(conn, qid)
    tags = all_tags_with_origin(conn, qid)
    err_tags = conn.execute(
        "SELECT tag, diagnosis_source FROM student_error_tags WHERE question_id=?", (qid,)
    ).fetchall()
    all_tags = config.REASONING_TAGS
    return templates.TemplateResponse(
        request,
        "tags_admin.html",
        {
            "q": q,
            "tags": tags,
            "err_tags": err_tags,
            "all_tags": all_tags,
        },
    )


@app.post("/admin/tags/{qid}")
def admin_tags_save(
    request: Request,
    qid: int,
    tag: str = Form(...),
    action: str = Form("add"),
    origin: str = Form("manual"),
    conn=Conn,
):
    if action == "add":
        set_manual(conn, qid, tag)
    else:
        # suppression, not deletion: re-tagging must not resurrect the tag,
        # and the sampler and weakness model must both stop seeing it
        suppress(conn, qid, tag)
    # weakness_cache is keyed on tag associations that just changed, and both
    # /weaknesses and select_drill read it in preference to recomputing. A
    # correction that leaves the cache alone is only half applied.
    compute_weakness(conn)
    _commit(conn)
    return RedirectResponse(f"/admin/tags/{qid}", status_code=303)


# ----------------------------------------------------------------- reports ----
# The database owns the scan boundary and completed report registry. HTML is a
# disposable rendering, so the route never shells out to a generator script.


@app.get("/reports", response_class=HTMLResponse)
def reports_index(request: Request, conn=Conn):
    status = request.query_params.get("status", "") if request else ""
    messages = {
        "empty": "Nothing new to review.",
        "historical": "Only imported historical attempts were new; checkpoint updated.",
        "failed": "The last report generation failed; try again.",
        "in_progress": "Generation already in progress.",
    }
    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "report": reports_mod.report_meta(conn),
            "generation_in_progress": reports_mod.generation_in_progress(conn),
            "status_message": messages.get(status),
            "nav": "reports",
        },
    )


@app.post("/reports/generate")
def reports_generate(request: Request, conn=Conn):
    result = reports_mod.run_report_generation(conn)
    if result.status == reports_mod.COMPLETED and result.report_name:
        return RedirectResponse(f"/reports/{result.report_name}", status_code=303)
    status = {
        reports_mod.NO_OP: "historical"
        if result.through_attempt_id > result.after_attempt_id
        else "empty",
        reports_mod.IN_PROGRESS: "in_progress",
        reports_mod.FAILED: "failed",
    }.get(result.status, "failed")
    return RedirectResponse(f"/reports?status={status}", status_code=303)


@app.get("/reports/{name}")
def reports_serve(request: Request, name: str, conn=Conn):
    # Serve only a report registered by a completed run. This is stricter than
    # trusting the newest filesystem mtime: orphaned HTML is never latest.
    base = reports_mod.REPORTS_DIR.resolve()
    if not reports_mod.is_completed_report(conn, name):
        raise HTTPException(status_code=404)
    if "/" in name or "\\" in name or not name.endswith(".html"):
        raise HTTPException(status_code=404)
    path = (base / name).resolve()
    # Exact parent match: a symlink or mount point under data/ that resolves
    # outside the reports dir fails the equality check (startswith would pass it).
    if path.parent != base or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="text/html")
