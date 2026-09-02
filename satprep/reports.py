"""Durable, incremental wrong-answer report generation.

The database owns the scan boundary and completed-run registry.  HTML is a
disposable rendering: it is written to a temporary file and renamed before a
completed run records its name in SQLite.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import html
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .clock import utc_now
from .config import REPO_ROOT
from .db import db_context

REPORTS_DIR = REPO_ROOT / "data" / "reports"
REPORT_LEASE_SECONDS = 15 * 60

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
NO_OP = "no_op"
IN_PROGRESS = "in_progress"

_REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.html$")


@dataclass(frozen=True)
class ReportGenerationResult:
    """The durable outcome and bounded interval of one generation request."""

    status: str
    run_id: int | None = None
    after_attempt_id: int = 0
    through_attempt_id: int = 0
    eligible_attempt_count: int = 0
    wrong_count: int = 0
    report_name: str | None = None
    report_path: Path | None = None
    generated_at: str | None = None
    error: str | None = None
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "after_attempt_id": self.after_attempt_id,
            "through_attempt_id": self.through_attempt_id,
            "eligible_attempt_count": self.eligible_attempt_count,
            "wrong_count": self.wrong_count,
            "report_name": self.report_name,
            "generated_at": self.generated_at,
            "error": self.error,
            "message": self.message,
        }


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _timestamp_after(seconds: int) -> str:
    return (
        (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=seconds))
        .replace(microsecond=0)
        .isoformat()
    )


def _default_report_name(suffix: str | int) -> str:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S")
    return f"weekly-{stamp}-{suffix}.html"


def _display_time(value: str | None) -> str:
    if not value:
        return "unknown"
    try:
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value)


def _time_label(time_ms) -> str:
    try:
        seconds = max(0, int(time_ms or 0)) // 1000
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _time_bucket(time_ms) -> str:
    try:
        seconds = int(time_ms or 0) / 1000
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "not recorded"
    if seconds < 30:
        return "under 30s"
    if seconds < 60:
        return "30–59s"
    if seconds < 120:
        return "1–2m"
    return "over 2m"


def _paragraphs(value) -> str:
    parts = [part.strip() for part in str(value or "").split("\n\n") if part.strip()]
    return "".join(f"<p>{_esc(part)}</p>" for part in parts) or "<p>—</p>"


def _choices(raw) -> list[dict]:
    try:
        choices = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return choices if isinstance(choices, list) else []


def _counter_rows(counter: collections.Counter) -> str:
    if not counter:
        return '<tr><td colspan="2">—</td></tr>'
    return "".join(
        f'<tr><td>{_esc(label)}</td><td class="num">{count}</td></tr>'
        for label, count in counter.most_common()
    )


def _render_wrong(row: dict) -> str:
    choices = []
    for choice in _choices(row.get("choices_json")):
        letter = str(choice.get("letter", ""))
        classes = ""
        label = ""
        if letter == (row.get("chosen_letter") or ""):
            classes = " wrong"
            label = '<span class="pill wrong">your answer</span>'
        if letter == (row.get("correct_letter") or ""):
            classes += " right"
            label += '<span class="pill right">correct</span>'
        choices.append(
            f'<div class="choice{classes}"><b>{_esc(letter)}</b> '
            f"{_esc(choice.get('text', ''))} {label}</div>"
        )
    choices_html = "".join(choices) or "<p>Choices unavailable.</p>"
    return f"""
<article class="question">
  <header><b>{_esc(row.get("official_skill") or "Unspecified skill")}</b>
    <span>{_esc(row.get("source_test"))} {_esc(row.get("source_question_number"))} ·
      {_display_time(row.get("attempted_at"))}</span></header>
  <div class="metrics">{_esc(row.get("official_domain") or "Unspecified domain")} ·
    time <b>{_time_label(row.get("time_ms"))}</b> · confidence {int(row.get("confidence") or 0)}</div>
  <div class="cols">
    <section><h4>Passage</h4>{_paragraphs(row.get("passage"))}</section>
    <section><h4>Question</h4><p class="stem">{_esc(row.get("stem"))}</p>
      <h4>Choices</h4>{choices_html}</section>
  </div>
  <div class="diagnosis"><h4>Review prompt</h4>
    <p>Compare the evidence, scope, and direction of your choice with the correct
    answer before reading the official rationale.</p>
    <h4>Official rationale</h4>{_paragraphs(row.get("rationale"))}
  </div>
</article>"""


def _render_report(
    rows: list[dict], *, after_attempt_id: int, through_attempt_id: int, generated_at: str
) -> str:
    wrong = [row for row in rows if not row.get("correct")]
    domains = collections.Counter(row.get("official_domain") or "Unspecified" for row in rows)
    skills = collections.Counter(row.get("official_skill") or "Unspecified" for row in rows)
    modules = collections.Counter(row.get("module") or "Unspecified" for row in rows)
    timing = collections.Counter(_time_bucket(row.get("time_ms")) for row in rows)
    confidence = collections.Counter(
        str(row.get("confidence") or 0) if row.get("confidence") else "not recorded" for row in rows
    )
    accuracy = round(100 * (len(rows) - len(wrong)) / len(rows), 1) if rows else 0.0
    cards = "".join(_render_wrong(row) for row in wrong)
    if not cards:
        cards = '<div class="callout">No mistakes in this interval. The eligible attempts still count in the trend tables above.</div>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SAT Wrong-Answer Review</title><style>
:root{{--ink:#182231;--mut:#5b6676;--line:#e3e7ed;--paper:#fafbfc;--card:#fff;--brand:#2f5b8f;--wrong:#a93226;--right:#197347;--code:#f2f5f8}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
.wrap{{max-width:1080px;margin:auto;padding:0 20px 64px}} .hero{{background:linear-gradient(135deg,#1f3a5c,#2f5b8f);color:white;padding:32px 0;margin-bottom:22px}}
h1{{margin:0 0 5px;font-size:28px}} h2{{margin:34px 0 10px;border-bottom:2px solid var(--brand);padding-bottom:5px;font-size:21px}}
h4{{margin:0 0 6px;color:var(--mut);font-size:12px;letter-spacing:.05em;text-transform:uppercase}}
.sub{{opacity:.94}} .grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}} @media(max-width:760px){{.grid{{grid-template-columns:1fr}}}}
.stat,table,.question,.callout{{background:var(--card);border:1px solid var(--line);border-radius:10px}}
.stat{{padding:12px 14px}} .stat .label{{color:var(--mut);font-size:12px;text-transform:uppercase}} .stat .value{{font-size:25px;font-weight:700}}
table{{width:100%;border-collapse:collapse;overflow:hidden}} th,td{{padding:8px 11px;border-bottom:1px solid var(--line);text-align:left}} th{{background:var(--code);color:var(--mut);font-size:12px;text-transform:uppercase}} td.num{{text-align:right;font-weight:700}}
.question{{margin:18px 0;overflow:hidden}} .question header{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 15px;border-bottom:1px solid var(--line)}}
.metrics{{padding:8px 15px;color:var(--mut);font-size:13px}} .cols{{display:grid;grid-template-columns:1.1fr 1fr}} @media(max-width:800px){{.cols{{grid-template-columns:1fr}}}}
.cols section{{padding:14px 15px}} .cols section+section{{border-left:1px solid var(--line)}} @media(max-width:800px){{.cols section+section{{border-left:0;border-top:1px solid var(--line)}}}}
.stem{{font-weight:600}} .choice{{padding:7px 9px;border:1px solid var(--line);border-radius:7px;margin:6px 0}} .choice.wrong{{background:#fdecea;border-color:#efb5ac}} .choice.right{{background:#e6f5ed;border-color:#a9dcc2}}
.pill{{display:inline-block;border-radius:20px;padding:1px 7px;margin-left:5px;font-size:11px;font-weight:600}} .pill.wrong{{color:var(--wrong);background:#fdecea}} .pill.right{{color:var(--right);background:#e6f5ed}}
.diagnosis{{background:var(--code);border-top:1px solid var(--line);padding:14px 15px}} .diagnosis p{{margin:0 0 10px}} .callout{{padding:13px 15px;margin:12px 0}} footer{{color:var(--mut);font-size:12px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body><div class="hero"><div class="wrap"><div>satprep · periodic review</div>
<h1>Wrong-Answer Review</h1><div class="sub">Eligible attempts {after_attempt_id + 1}–{through_attempt_id}; generated {_esc(_display_time(generated_at))}.</div></div></div>
<main class="wrap"><h2>At a glance</h2><div class="grid">
<div class="stat"><div class="label">Eligible attempts</div><div class="value">{len(rows)}</div></div>
<div class="stat"><div class="label">Wrong answers</div><div class="value">{len(wrong)}</div></div>
<div class="stat"><div class="label">Accuracy</div><div class="value">{accuracy}%</div></div></div>
<h2>All eligible attempts</h2><div class="grid"><div><h4>By domain</h4><table><tr><th>Domain</th><th>#</th></tr>{_counter_rows(domains)}</table></div>
<div><h4>By skill</h4><table><tr><th>Skill</th><th>#</th></tr>{_counter_rows(skills)}</table></div>
<div><h4>By module</h4><table><tr><th>Module</th><th>#</th></tr>{_counter_rows(modules)}</table></div></div>
<div class="grid" style="margin-top:12px"><div><h4>Timing</h4><table><tr><th>Bucket</th><th>#</th></tr>{_counter_rows(timing)}</table></div>
<div><h4>Confidence</h4><table><tr><th>Level</th><th>#</th></tr>{_counter_rows(confidence)}</table></div><div></div></div>
<h2>Question-by-question review</h2>{cards}</main><footer class="wrap">The database is authoritative; this HTML is a disposable rendering. The strategy framework is instructor guidance, not College Board policy.</footer></body></html>"""


def _validate_report_name(name: str | Path) -> str:
    value = str(name)
    if not _REPORT_NAME.fullmatch(value) or Path(value).name != value:
        raise ValueError("report name must be a simple .html filename")
    return value


def _report_path(name: str, reports_dir: Path) -> Path:
    base = reports_dir.resolve()
    path = (base / _validate_report_name(name)).resolve()
    if path.parent != base:
        raise ValueError("report path escaped the reports directory")
    return path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def _eligible_rows(conn, after_attempt_id: int, through_attempt_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT a.id AS attempt_id, a.chosen_letter, a.correct, a.confidence, a.time_ms,
                  a.mode, a.attempted_at, q.fingerprint, q.source_test,
                  q.source_question_number, q.module, q.passage, q.stem,
                  q.choices_json, q.correct_letter, q.rationale,
                  q.official_domain, q.official_skill, q.difficulty
           FROM attempts a JOIN questions q ON q.id=a.question_id
           WHERE a.id > ? AND a.id <= ?
             AND COALESCE(a.mode, '') != 'historical'
           ORDER BY a.id""",
        (after_attempt_id, through_attempt_id),
    ).fetchall()
    return [dict(row) for row in rows]


def generate_error_report(
    conn,
    *,
    after_attempt_id: int,
    through_attempt_id: int,
    out_name: str | None = None,
    reports_dir: Path | None = None,
) -> ReportGenerationResult:
    """Generate one fixed attempt interval without changing database state.

    The caller owns the report-run lease and commits the returned outcome.  No
    query here can see attempts beyond ``through_attempt_id``.
    """
    after_attempt_id = int(after_attempt_id)
    through_attempt_id = int(through_attempt_id)
    if after_attempt_id < 0 or through_attempt_id < after_attempt_id:
        raise ValueError("attempt interval must satisfy 0 <= after <= through")
    window = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN COALESCE(mode, '') != 'historical' THEN 1 ELSE 0 END), 0) AS eligible
           FROM attempts WHERE id > ? AND id <= ?""",
        (after_attempt_id, through_attempt_id),
    ).fetchone()
    total = int(window["total"] or 0)
    expected_eligible = int(window["eligible"] or 0)
    rows = _eligible_rows(conn, after_attempt_id, through_attempt_id)
    eligible = len(rows)
    if eligible != expected_eligible:
        raise RuntimeError("report interval contains an attempt without a question")
    if not eligible:
        message = (
            "Only imported historical attempts were new; checkpoint updated."
            if total
            else "Nothing new to review."
        )
        return ReportGenerationResult(
            status=NO_OP,
            after_attempt_id=after_attempt_id,
            through_attempt_id=through_attempt_id,
            eligible_attempt_count=0,
            wrong_count=0,
            generated_at=utc_now(),
            message=message,
        )
    generated_at = utc_now()
    name = out_name or _default_report_name(uuid.uuid4().hex[:8])
    reports_dir = Path(reports_dir or REPORTS_DIR)
    if conn.execute(
        "SELECT 1 FROM report_runs WHERE status='completed' AND report_name=? LIMIT 1", (name,)
    ).fetchone():
        raise ValueError("report name is already registered")
    path = _report_path(name, reports_dir)
    _atomic_write(
        path,
        _render_report(
            rows,
            after_attempt_id=after_attempt_id,
            through_attempt_id=through_attempt_id,
            generated_at=generated_at,
        ),
    )
    wrong_count = sum(1 for row in rows if not row.get("correct"))
    return ReportGenerationResult(
        status=COMPLETED,
        after_attempt_id=after_attempt_id,
        through_attempt_id=through_attempt_id,
        eligible_attempt_count=eligible,
        wrong_count=wrong_count,
        report_name=path.name,
        report_path=path,
        generated_at=generated_at,
        message="Report generated.",
    )


def get_report_watermark(conn) -> int:
    """Return the last successfully scanned attempt id."""
    row = conn.execute("SELECT committed_attempt_id FROM report_checkpoint WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO report_checkpoint (id, committed_attempt_id) VALUES (1, 0)")
        return 0
    return int(row["committed_attempt_id"])


def _error_text(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:2000]


def _acquire_generation(conn, lease_seconds: int) -> dict | ReportGenerationResult:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    now = utc_now()
    conn.execute(
        """UPDATE report_runs
           SET status='failed', completed_at=?, error=COALESCE(error, ?),
               lease_expires_at=NULL, lease_token=NULL
           WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
        (now, "stale report generation recovered", now),
    )
    active = conn.execute(
        """SELECT id, after_attempt_id, through_attempt_id
           FROM report_runs WHERE status='running' AND lease_expires_at > ?
           ORDER BY id DESC LIMIT 1""",
        (now,),
    ).fetchone()
    if active:
        conn.rollback()
        return ReportGenerationResult(
            status=IN_PROGRESS,
            run_id=active["id"],
            after_attempt_id=active["after_attempt_id"],
            through_attempt_id=active["through_attempt_id"],
            message="Generation already in progress.",
        )
    after = get_report_watermark(conn)
    through = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts").fetchone()[0])
    eligible = int(
        conn.execute(
            """SELECT COUNT(*) FROM attempts
               WHERE id > ? AND id <= ? AND COALESCE(mode, '') != 'historical'""",
            (after, through),
        ).fetchone()[0]
    )
    wrong = int(
        conn.execute(
            """SELECT COUNT(*) FROM attempts
               WHERE id > ? AND id <= ? AND COALESCE(mode, '') != 'historical' AND correct=0""",
            (after, through),
        ).fetchone()[0]
    )
    token = uuid.uuid4().hex
    cursor = conn.execute(
        """INSERT INTO report_runs
           (status, started_at, after_attempt_id, through_attempt_id,
            eligible_attempt_count, wrong_count, lease_expires_at, lease_token)
           VALUES ('running', ?, ?, ?, ?, ?, ?, ?)""",
        (now, after, through, eligible, wrong, _timestamp_after(lease_seconds), token),
    )
    run_id = int(cursor.lastrowid)
    conn.commit()
    return {
        "run_id": run_id,
        "lease_token": token,
        "after_attempt_id": after,
        "through_attempt_id": through,
        "eligible_attempt_count": eligible,
        "wrong_count": wrong,
    }


def _finish_generation(conn, acquired: dict, result: ReportGenerationResult) -> bool:
    if result.status not in {COMPLETED, NO_OP}:
        raise RuntimeError(f"unexpected report generation status: {result.status}")
    if (
        result.after_attempt_id != acquired["after_attempt_id"]
        or result.through_attempt_id != acquired["through_attempt_id"]
    ):
        raise RuntimeError("report generator changed its fixed attempt interval")
    if result.status == COMPLETED and (
        not result.report_name or not result.report_path or not result.report_path.is_file()
    ):
        raise RuntimeError("completed report has no durable HTML artifact")
    conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT status, lease_token, after_attempt_id FROM report_runs WHERE id=?",
        (acquired["run_id"],),
    ).fetchone()
    if not row or row["status"] != RUNNING or row["lease_token"] != acquired["lease_token"]:
        conn.rollback()
        return False
    watermark = get_report_watermark(conn)
    if watermark != acquired["after_attempt_id"]:
        conn.rollback()
        return False
    status = COMPLETED if result.status == COMPLETED else NO_OP
    conn.execute(
        "UPDATE report_checkpoint SET committed_attempt_id=? WHERE id=1",
        (max(watermark, result.through_attempt_id),),
    )
    updated = conn.execute(
        """UPDATE report_runs
           SET status=?, completed_at=?, eligible_attempt_count=?, wrong_count=?,
               report_name=?, error=NULL, lease_expires_at=NULL, lease_token=NULL
           WHERE id=? AND status='running' AND lease_token=?""",
        (
            status,
            result.generated_at or utc_now(),
            result.eligible_attempt_count,
            result.wrong_count,
            result.report_name,
            acquired["run_id"],
            acquired["lease_token"],
        ),
    ).rowcount
    if updated != 1:
        conn.rollback()
        return False
    conn.commit()
    return True


def _fail_generation(conn, acquired: dict, exc: Exception) -> None:
    try:
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE report_runs
               SET status='failed', completed_at=?, error=?,
                   lease_expires_at=NULL, lease_token=NULL
               WHERE id=? AND status='running' AND lease_token=?""",
            (utc_now(), _error_text(exc), acquired["run_id"], acquired["lease_token"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()


def run_report_generation(
    conn,
    *,
    out_name: str | None = None,
    reports_dir: Path | None = None,
    lease_seconds: int = REPORT_LEASE_SECONDS,
) -> ReportGenerationResult:
    """Acquire the database lease, generate, then commit the watermark."""
    acquired = _acquire_generation(conn, lease_seconds)
    if isinstance(acquired, ReportGenerationResult):
        return acquired
    result: ReportGenerationResult | None = None
    try:
        if acquired["eligible_attempt_count"]:
            # Include the run id in the default filename so same-second
            # requests cannot overwrite a previous rendering.
            name = out_name or _default_report_name(acquired["run_id"])
            result = generate_error_report(
                conn,
                after_attempt_id=acquired["after_attempt_id"],
                through_attempt_id=acquired["through_attempt_id"],
                out_name=name,
                reports_dir=reports_dir,
            )
        else:
            result = ReportGenerationResult(
                status=NO_OP,
                after_attempt_id=acquired["after_attempt_id"],
                through_attempt_id=acquired["through_attempt_id"],
                generated_at=utc_now(),
                message=(
                    "Only imported historical attempts were new; checkpoint updated."
                    if acquired["through_attempt_id"] > acquired["after_attempt_id"]
                    else "Nothing new to review."
                ),
            )
        result = replace(result, run_id=acquired["run_id"])
        if not _finish_generation(conn, acquired, result):
            raise RuntimeError("report generation lease was lost before commit")
        return result
    except Exception as exc:
        if result and result.report_path:
            with contextlib.suppress(OSError):
                result.report_path.unlink(missing_ok=True)
        _fail_generation(conn, acquired, exc)
        return ReportGenerationResult(
            status=FAILED,
            run_id=acquired["run_id"],
            after_attempt_id=acquired["after_attempt_id"],
            through_attempt_id=acquired["through_attempt_id"],
            eligible_attempt_count=acquired["eligible_attempt_count"],
            wrong_count=acquired["wrong_count"],
            error=_error_text(exc),
            message="Report generation failed; try again.",
        )


def generation_in_progress(conn) -> bool:
    """Whether a non-stale generation lease is currently held."""
    return bool(
        conn.execute(
            "SELECT 1 FROM report_runs WHERE status='running' AND lease_expires_at > ? LIMIT 1",
            (utc_now(),),
        ).fetchone()
    )


def _completed_rows(conn):
    return conn.execute(
        """SELECT id, completed_at, report_name, eligible_attempt_count, wrong_count
           FROM report_runs
           WHERE status='completed' AND report_name IS NOT NULL
           ORDER BY completed_at DESC, id DESC"""
    ).fetchall()


def latest_report(conn=None) -> Path | None:
    """Latest completed report whose registered file still exists."""
    if conn is None:
        with db_context() as owned:
            return latest_report(owned)
    for row in _completed_rows(conn):
        try:
            path = _report_path(row["report_name"], REPORTS_DIR)
        except (TypeError, ValueError):
            continue
        if path.is_file():
            return path
    return None


def report_meta(conn=None) -> dict | None:
    """Display metadata from the newest completed run, never from mtime."""
    if conn is None:
        with db_context() as owned:
            return report_meta(owned)
    for row in _completed_rows(conn):
        try:
            path = _report_path(row["report_name"], REPORTS_DIR)
        except (TypeError, ValueError):
            continue
        if not path.is_file():
            continue
        return {
            "name": path.name,
            "url": f"/reports/{path.name}",
            "generated": _display_time(row["completed_at"]).removesuffix(" UTC"),
            "eligible_attempt_count": row["eligible_attempt_count"],
            "wrong_count": row["wrong_count"],
        }
    return None


def is_completed_report(conn, name: str) -> bool:
    try:
        name = _validate_report_name(name)
    except ValueError:
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM report_runs WHERE status='completed' AND report_name=? LIMIT 1",
            (name,),
        ).fetchone()
    )


def build_report(
    since: str | None = None, out_name: str | None = None, *, conn=None
) -> Path | None:
    """Compatibility wrapper for offline callers; new callers use the lease API."""
    owned = conn is None
    if owned:
        with db_context() as opened:
            return build_report(since, out_name, conn=opened)
    after = 0
    if since:
        after = int(
            conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM attempts WHERE attempted_at <= ?", (since,)
            ).fetchone()[0]
        )
    through = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts").fetchone()[0])
    result = generate_error_report(
        conn,
        after_attempt_id=after,
        through_attempt_id=through,
        out_name=out_name,
    )
    return result.report_path


def cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate an incremental SAT wrong-answer report")
    parser.add_argument("--out", help="output filename under data/reports/")
    # Retain the old switches so existing scripts fail neither at parse time
    # nor by creating a filesystem checkpoint. The database watermark is now
    # authoritative, so these options intentionally do not alter the scan.
    parser.add_argument("--days", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--since", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    with db_context() as conn:
        result = run_report_generation(conn, out_name=args.out)
    if result.status == COMPLETED:
        print(f"wrote {result.report_path}")
    else:
        print(result.message or result.error or result.status)
    return 0 if result.status in {COMPLETED, NO_OP, IN_PROGRESS} else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli_main())
