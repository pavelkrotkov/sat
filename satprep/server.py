"""Local web UI (FastAPI + Jinja2). Same DB and selection logic as the CLI.

Screens: dashboard, start drill, question, results, review, weaknesses,
history, fresh benchmark, admin (tag inspection/correction + selection
traces).
"""

import json

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .analytics import full_dashboard
from .db import connect
from .tags import all_tags_with_origin
from .weakness import cached_profile

app = FastAPI(title="satprep", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(config.REPO_ROOT / "satprep" / "static")), name="static")
app.mount("/figures", StaticFiles(directory=str(config.REPO_ROOT / "artifacts" / "images")), name="figures")
templates = Jinja2Templates(directory=str(config.REPO_ROOT / "satprep" / "templates"))
templates.env.filters["basename"] = lambda p: str(p).rsplit("/", 1)[-1]


def _q(conn, qid: int):
    row = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if row:
        row = dict(row)
        row["choices"] = json.loads(row.pop("choices_json"))
    return row


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    d = full_dashboard()
    return templates.TemplateResponse(request, "dashboard.html", {"d": d,
    })


@app.get("/start", response_class=HTMLResponse)
def start_drill(request: Request):
    from .config import REASONING_TAGS

    focus = request.query_params.get("focus") or ""
    conn = connect()
    weak_tags = list(cached_profile(conn, "tag"))[:12]
    counts = dict(conn.execute(
        "SELECT pool, COUNT(*) FROM questions WHERE active=1 GROUP BY pool"
    ).fetchall())
    conn.close()
    return templates.TemplateResponse(request, "start.html", {"weak_tags": weak_tags,
        "reasoning_tags": sorted(REASONING_TAGS),
        "pool_counts": counts,
        "focus": focus,
    })


@app.post("/begin")
def begin(request: Request, mode: str = Form(...), count: int = Form(0),
          focus_tag: str = Form("")):
    from .sessions import create_session

    sess = create_session(mode=mode, count=count or None,
                          focus_tag=focus_tag or None)
    sid = sess["plan"]["session_id"]
    return RedirectResponse(f"/question/{sid}/0", status_code=303)


@app.get("/question/{sid}/{idx}", response_class=HTMLResponse)
def question(request: Request, sid: str, idx: int):
    conn = connect()
    plan_row = conn.execute("SELECT plan_json FROM sessions WHERE id=?", (sid,)).fetchone()
    if not plan_row:
        conn.close()
        return RedirectResponse("/start", status_code=303)
    items = json.loads(plan_row["plan_json"])
    if idx >= len(items):
        conn.close()
        return RedirectResponse(f"/results/{sid}", status_code=303)
    q = _q(conn, items[idx]["question_id"])
    conn.close()
    return templates.TemplateResponse(request, "question.html", {"q": q, "sid": sid, "idx": idx,
        "total": len(items),
    })


@app.post("/answer/{sid}/{idx}")
async def answer(request: Request, sid: str, idx: int,
                 question_id: int = Form(...), letter: str = Form(...),
                 confidence: int = Form(...), elapsed_ms: int = Form(0)):
    from .sessions import submit_answer

    submit_answer(sid, question_id, letter, confidence, elapsed_ms)
    return RedirectResponse(f"/question/{sid}/{idx + 1}", status_code=303)


@app.get("/results/{sid}", response_class=HTMLResponse)
def results(request: Request, sid: str):
    from .sessions import complete_session

    summary = complete_session(sid)  # idempotent-ish; refreshes weakness cache
    return templates.TemplateResponse(request, "results.html", {"summary": summary, "sid": sid,
    })


@app.get("/review/{sid}", response_class=HTMLResponse)
def review(request: Request, sid: str):
    from .sessions import review_payload

    rows = review_payload(sid)
    return templates.TemplateResponse(request, "review.html", {"reviews": rows, "sid": sid,
    })


@app.get("/weaknesses", response_class=HTMLResponse)
def weaknesses(request: Request):
    conn = connect()
    skills = cached_profile(conn, "skill")
    tags = cached_profile(conn, "tag")
    conn.close()

    def rows(profile):
        return [(entity, p["score"], {k: v for k, v in p.items() if k != "score"})
                for entity, p in profile.items()]

    return templates.TemplateResponse(request, "weaknesses.html", {"skills": rows(skills),
        "tags": rows(tags),
    })


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    conn = connect()
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
    conn.close()
    return templates.TemplateResponse(request, "history.html", {"sessions": sessions, "attempts": attempts,
    })


@app.get("/benchmark", response_class=HTMLResponse)
def benchmark_start(request: Request):
    conn = connect()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM questions WHERE pool='protected_benchmark' AND seen_benchmark=0"
    ).fetchone()[0]
    conn.close()
    return templates.TemplateResponse(request, "benchmark.html", {"remaining": remaining,
    })


@app.post("/benchmark/begin")
def benchmark_begin(request: Request, count: int = Form(8)):
    from .sessions import create_session

    sess = create_session(mode="fresh_benchmark", count=min(count, 25))
    sid = sess["plan"]["session_id"]
    if not sess["questions"]:
        return RedirectResponse("/benchmark", status_code=303)
    return RedirectResponse(f"/question/{sid}/0", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    conn = connect()
    recent_sessions = conn.execute(
        "SELECT id, mode, created_at, seed, algo_version FROM sessions ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(request, "admin.html", {"sessions": recent_sessions,
    })


@app.get("/admin/why/{sid}", response_class=HTMLResponse)
def admin_why(request: Request, sid: str):
    conn = connect()
    plan_row = conn.execute("SELECT plan_json, mode, seed FROM sessions WHERE id=?", (sid,)).fetchone()
    if not plan_row:
        conn.close()
        return RedirectResponse("/admin", status_code=303)
    items = json.loads(plan_row["plan_json"])
    enriched = []
    for it in items:
        q = conn.execute(
            "SELECT official_skill, source_test, source_question_number, pool, difficulty FROM questions WHERE id=?",
            (it["question_id"],),
        ).fetchone()
        enriched.append({**it, "meta": dict(q) if q else {}})
    conn.close()
    return templates.TemplateResponse(request, "why.html", {"items": enriched, "sid": sid,
        "mode": plan_row["mode"], "seed": plan_row["seed"],
    })


@app.get("/admin/tags/{qid}", response_class=HTMLResponse)
def admin_tags(request: Request, qid: int):
    conn = connect()
    q = _q(conn, qid)
    tags = all_tags_with_origin(conn, qid)
    err_tags = conn.execute(
        "SELECT tag, diagnosis_source FROM student_error_tags WHERE question_id=?", (qid,)
    ).fetchall()
    all_tags = config.REASONING_TAGS
    conn.close()
    return templates.TemplateResponse(request, "tags_admin.html", {"q": q, "tags": tags, "err_tags": err_tags,
        "all_tags": all_tags,
    })


@app.post("/admin/tags/{qid}")
async def admin_tags_save(request: Request, qid: int, tag: str = Form(...),
                          action: str = Form("add"), origin: str = Form("manual")):
    from .tags import set_manual, suppress
    from .weakness import compute_weakness

    conn = connect()
    if action == "add":
        set_manual(conn, qid, tag)
    else:
        # suppression, not deletion: re-tagging must not resurrect the tag,
        # and the sampler and weakness model must both stop seeing it
        suppress(conn, qid, tag)
    conn.commit()
    # weakness_cache is keyed on tag associations that just changed, and both
    # /weaknesses and select_drill read it in preference to recomputing. A
    # correction that leaves the cache alone is only half applied.
    compute_weakness(conn)
    conn.close()
    return RedirectResponse(f"/admin/tags/{qid}", status_code=303)
