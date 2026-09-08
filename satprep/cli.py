"""satprep command line interface.

satprep ingest              # rebuild corpus from outputs/ + imports/
satprep analyze             # (re)compute tags + weakness profile
satprep drill [--count N] [--focus TAG] [--mode MODE]
satprep benchmark           # protected fresh benchmark
satprep stats               # dashboard numbers in terminal
satprep serve [--host H] [--port P]   # web UI (loopback unless told otherwise)
"""

import argparse
import ipaddress
import json
import pathlib
import sqlite3
import sys
from datetime import datetime
from urllib.parse import quote

from bs4 import BeautifulSoup

from . import config
from .analytics import full_dashboard
from .corpus.archive import (
    ARCHIVE_VERSION,
    LEGACY_ARCHIVE_VERSIONS,
    export_corpus,
    restore_corpus,
)
from .corpus.audit import audit_bluebook, audit_passes
from .corpus.ingest import ingest_bluebook, ingest_qbank
from .corpus.qbank_fetch import backfill_visuals, fetch_qbank
from .corpus.repair import repair_bluebook
from .corpus.tagger import run_full_tagging
from .db import db_context
from .explanations import explain_error
from .reviews import (
    APPROVED,
    REJECTED,
    ReviewStateError,
    delete_review,
    edit_review,
    ensure_review_schema,
    export_review,
    get_review,
    list_deletions,
    list_reviews,
    transition,
    upsert_draft,
)
from .training.remediation import build_remediation_plan, explain_selection
from .training.sessions import (
    complete_session,
    create_session,
    excerpt_sentences,
    review_payload,
    submit_answer,
)
from .training.weakness import compute_weakness


def _cell_text(table_html: str) -> list[str]:
    """Render every sanitized table cell row-by-row for the terminal drill."""
    soup = BeautifulSoup(table_html, "html.parser")
    out = []
    for tag in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tag.find_all(["th", "td"])]
        if cells:
            out.append(" | ".join(cells))
    return out


def _auto_export(conn) -> None:
    """Snapshot the corpus, after making the ingest durable.

    export_corpus replaces the archive in place. Publishing it while the
    transaction is still open risks an archive holding rows the database
    later rolls back - the archive is the only copy of question content, so
    it must never run ahead of the corpus it claims to snapshot.
    """
    conn.commit()
    path = export_corpus(conn)
    print(f"archive: {path.name}")


def cmd_ingest(args) -> None:
    with db_context() as conn:
        print(f"bluebook history: {ingest_bluebook(conn)}")
        print(f"question bank imports: {ingest_qbank(conn)}")
        print(f"tagging: {run_full_tagging(conn)}")
        _auto_export(conn)


def cmd_repair_bluebook(args) -> None:
    """Reconcile the historical corpus from snapshots + JSON (issue #49).

    Idempotent: re-running reports the same (small) change counts. After a
    repair, run `satprep ingest` to regenerate the archive so exports/
    reflects the repaired rows.
    """
    with db_context() as conn:
        stats = repair_bluebook(conn)
    warnings = stats.pop("warnings", [])
    for w in warnings[:20]:
        print(f"  warn: {w}")
    if len(warnings) > 20:
        print(f"  ... and {len(warnings) - 20} more field warnings")
    print(json.dumps(stats, indent=2))


def cmd_audit_bluebook(args) -> None:
    """Print the Bluebook corpus audit and exit non-zero on gate failures."""
    with db_context() as conn:
        report = audit_bluebook(conn)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not audit_passes(report):
        raise SystemExit(f"audit failed: {len(report['failures'])} gate(s) not met")
    print("audit: PASS")


def cmd_export(args) -> None:
    # Checked here rather than in export_corpus, which now receives an open
    # connection and so cannot tell a missing database from an empty one.
    # Applies to --out too: db_context would otherwise create an empty
    # database and report a successful export of nothing.
    if not config.DB_PATH.exists():
        raise SystemExit(
            f"No database at {config.DB_PATH}; refusing to overwrite the archive. "
            f"Run `satprep restore` first if you intended a rebuild."
        )
    with db_context() as conn:
        path = export_corpus(conn, out_path=pathlib.Path(args.out) if args.out else None)
    print(f"exported corpus to {path}")


def cmd_restore(args) -> None:
    # Resolved and checked before db_context, which would otherwise create an
    # empty database on the way to reporting a missing archive - and that
    # empty database then satisfies cmd_export's guard.
    explicit = pathlib.Path(args.file) if args.file else None
    if explicit is not None:
        archive = explicit
    else:
        archive = config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl"
        # Upgraded hosts may only have v1; explicit paths never fall back.
        if not archive.exists():
            legacy = config.REPO_ROOT / "exports" / f"corpus-v{min(LEGACY_ARCHIVE_VERSIONS)}.jsonl"
            if legacy.exists():
                archive = legacy
    if not archive.exists():
        raise SystemExit(f"No archive at {archive}")
    with db_context() as conn:
        stats = restore_corpus(conn, archive_path=archive)
    print(f"restore: {stats}")


def cmd_analyze(args) -> None:
    with db_context() as conn:
        scores = compute_weakness(conn)
    for etype in ("skill", "tag"):
        ranked = sorted(scores[etype].items(), key=lambda kv: -kv[1]["score"])[:12]
        print(f"\n=== {etype} weaknesses ===")
        for name, p in ranked:
            n = p.get("wrong", 0) + p.get("correct", 0)
            print(f"{p['score']:5.1f}  {name:<38} {p.get('wrong', 0)}w/{n}n")


def _interactive_answer() -> tuple[str, int]:
    raw = input("answer (letter + confidence 1-3, e.g. 'B 2'): ").strip().split()
    letter = raw[0].upper()[:1] if raw else ""
    conf = int(raw[1]) if len(raw) > 1 and raw[1].isdigit() else 2
    return letter, conf


def cmd_drill(args) -> None:
    mode = args.mode or "targeted_drill"
    # One connection for the whole drill: selection, every answer, scoring and
    # the review pass are one unit of work, so an interrupted drill does not
    # leave a session row behind with orphaned attempts.
    with db_context() as conn:
        _run_drill(conn, mode, args)


def _run_drill(conn, mode: str, args) -> None:  # noqa: C901 (legacy: interactive print orchestration)
    sess = create_session(conn, mode=mode, count=args.count, seed=args.seed, focus_tag=args.focus)
    plan, questions = sess["plan"], sess["questions"]
    if not questions:
        print("No questions available for this mode. Run 'satprep ingest' first.")
        return
    sid = plan["session_id"]
    print(f"\n== {mode} | session {sid} | {len(questions)} questions ==\n")
    for i, q in enumerate(questions, 1):
        print(f"--- Question {i}/{len(questions)} ---")
        if q["passage"]:
            print(q["passage"][:1800])
            print()
        visuals = q.get("visuals") or []
        if visuals:
            # Mixed table and image records render in source order.
            for v in visuals:
                if v.get("kind") == "image":
                    print(f"  [figure: {v.get('file')}]")
                elif v.get("kind") == "table":
                    print(f"  [table: {' | '.join(_cell_text(v.get('html', '')))}]")
        else:
            # legacy rows (pre-visuals) render their images directly
            for img in q.get("images") or []:
                print(f"  [figure: {img}]")
        print(q["stem"])
        for c in sorted(q["choices"], key=lambda c: c["letter"]):
            print(f"  {c['letter']}. {c['text'][:300]}")
        q_start = datetime.now().astimezone()  # spec section 12: per-question time
        letter, conf = _interactive_answer()
        ms = int((datetime.now().astimezone() - q_start).total_seconds() * 1000)
        res = submit_answer(conn, sid, q["id"], letter, conf, ms)
        if res.get("duplicate"):
            print("already answered - not recorded again\n")
            continue
        print(("correct" if res["correct"] else f"wrong (key: {res['key']})") + "\n")
    summary = complete_session(conn, sid)
    print("summary:", json.dumps(summary))
    reviews = [r for r in review_payload(conn, sid) if not r["correct"]]
    for r in reviews:
        print(
            f"\nREVIEW Q{r['question_id']} [{r['official_skill']}] trap={','.join(r['trap_tags'])}"
        )
        if r["passage_skeleton"]:
            print("skeleton:", " | ".join(s[:120] for s in r["passage_skeleton"]))
        if r["rationale_official"]:
            print("rationale:", excerpt_sentences(r["rationale_official"], max_chars=500))


def cmd_benchmark(args) -> None:
    args.mode = "fresh_benchmark"
    if not args.count:
        args.count = 8
    cmd_drill(args)


def cmd_fetch_qbank(args) -> None:
    domains = [d.strip().upper() for d in args.domains.split(",") if d.strip()] or None
    with db_context() as conn:
        # Audits skip fetch, tagging, and archive writes so they remain read-only.
        if args.audit_visuals:
            bstats = backfill_visuals(
                conn,
                hint=not args.full_sweep,
                limit=args.limit,
                sleep_s=args.sleep,
                audit_only=True,
            )
            print("audit:", json.dumps(bstats))
            return
        # NB: args.limit caps BOTH the fetch and the backfill. A `--limit 10`
        # fetch therefore caps the subsequent backfill at the same 10 rows
        # (in addition to any hint filter). Pass --full-sweep + a larger
        # --limit if you need a separate backfill scope.
        stats = fetch_qbank(
            conn, hard_only=args.hard_only, domains=domains, limit=args.limit, sleep_s=args.sleep
        )
        print("done:", json.dumps(stats))
        # Repair rows whose visuals were dropped before extraction existed:
        # re-fetch the stored imageless/visual-less bank questions and attach
        # them. Default is the stem-hint sweep; --full-sweep checks every one.
        bstats = backfill_visuals(
            conn,
            hint=not args.full_sweep,
            limit=args.limit,
            sleep_s=args.sleep,
            audit_only=False,
        )
        print("backfill:", json.dumps(bstats))
        # tag BEFORE snapshotting so the archive never stores tag-less rows
        print(f"tagging: {run_full_tagging(conn)}")
        _auto_export(conn)


def cmd_explain(args) -> None:
    """Run the KB-aware explanation pipeline for one question/attempt.

    Pure read-only: no DB writes, no schema migrations. The rule-based
    path is always available; the optional LLM upgrade fires only when
    SAT_EXPLAIN_API_KEY is set."""
    _DB_PATH = config.DB_PATH
    if not _DB_PATH.exists():
        raise SystemExit(
            f"no database at {_DB_PATH}; run `satprep ingest` first "
            "(this command is read-only and will not create one)"
        )
    # Open a connection in URI read-only mode so the command cannot
    # create or migrate the file. The pipeline inspects only `questions`
    # and (when resolving the most-recent wrong attempt) `attempts`,
    # neither of which is mutated here.
    # Encode URI-significant path characters before SQLite parses the path.
    db_uri = f"file:{quote(str(_DB_PATH))}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        qid = args.question_id
        row = conn.execute(
            "SELECT id, passage, stem, choices_json, correct_letter, "
            "fingerprint, rationale "
            "FROM questions WHERE id=? AND active=1",
            (qid,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"no active question with id={qid}")
        # Resolve the student's chosen letter: either the most recent
        # wrong attempt on this question, or the explicit --student-letter.
        student_letter = args.student_letter
        if student_letter is None:
            attempt = conn.execute(
                "SELECT chosen_letter FROM attempts "
                "WHERE question_id=? AND correct=0 "
                "ORDER BY id DESC LIMIT 1",
                (qid,),
            ).fetchone()
            if attempt is None:
                raise SystemExit(f"no wrong attempt for question_id={qid}; pass --student-letter")
            student_letter = attempt["chosen_letter"]
        # Normalise the student letter so downstream code always sees an
        # uppercase key present in the choice list and different from
        # the key. Down-casing, an unknown letter, or the correct answer
        # would otherwise produce a misleading "you chose the correct
        # answer" or a silently mismatched evidence row.
        choices_raw = json.loads(row["choices_json"])
        letters = {c.get("letter", "").upper() for c in choices_raw}
        student_letter = student_letter.upper().strip()
        if student_letter not in letters:
            raise SystemExit(
                f"--student-letter {student_letter!r} is not one of the choices {sorted(letters)}"
            )
        if student_letter == row["correct_letter"]:
            raise SystemExit(
                f"--student-letter {student_letter!r} equals the "
                f"correct answer; pass --student-letter with the wrong "
                "choice to explain an error"
            )
        ex = explain_error(
            question_id=qid,
            passage=row["passage"],
            stem=row["stem"],
            choices=choices_raw,
            student_letter=student_letter,
            correct_letter=row["correct_letter"],
            rationale=row["rationale"] or "",
            question_fingerprint=row["fingerprint"] or "",
            conn=conn,
        )
    finally:
        conn.close()
    print(
        json.dumps(
            {
                "question_id": qid,
                "student_letter": student_letter,
                "correct_letter": row["correct_letter"],
                "tested_task": ex.tested_task,
                "tempting_answer": ex.tempting_answer,
                "exact_failure": ex.exact_failure,
                "correct_reasoning": ex.correct_reasoning,
                "kb_tactic_refs": ex.kb_tactic_refs,
                "evidence_citations": ex.evidence_citations,
                "confidence": ex.confidence,
                "mode": ex.mode,
                "model": ex.model,
                "error_taxonomy": ex.error_taxonomy,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def cmd_review_generate(args) -> None:
    """Generate a draft review for a question from its most recent
    wrong attempt, using the #36 pipeline. Read-only on the KB; writes
    the review draft into SQLite."""
    with db_context() as conn:
        ensure_review_schema(conn)
        qid = args.question_id
        row = conn.execute(
            "SELECT id, passage, stem, choices_json, correct_letter, "
            "fingerprint, rationale "
            "FROM questions WHERE id=? AND active=1",
            (qid,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"no active question with id={qid}")
        attempt = conn.execute(
            "SELECT chosen_letter FROM attempts "
            "WHERE question_id=? AND correct=0 "
            "ORDER BY id DESC LIMIT 1",
            (qid,),
        ).fetchone()
        if attempt is None:
            raise SystemExit(f"no wrong attempt for question_id={qid}; nothing to review")
        choices = json.loads(row["choices_json"])
        ex = explain_error(
            question_id=qid,
            passage=row["passage"],
            stem=row["stem"],
            choices=choices,
            student_letter=attempt["chosen_letter"],
            correct_letter=row["correct_letter"],
            rationale=row["rationale"] or "",
            question_fingerprint=row["fingerprint"] or "",
            conn=conn,
        )
        rev = upsert_draft(
            conn,
            question_id=qid,
            question_fingerprint=row["fingerprint"] or "",
            generator=ex.mode,
            model=ex.model,
            tested_task=ex.tested_task,
            tempting_answer=ex.tempting_answer,
            exact_failure=ex.exact_failure,
            correct_reasoning=ex.correct_reasoning,
            kb_tactic_refs=ex.kb_tactic_refs,
            error_taxonomy=ex.error_taxonomy,
            evidence=ex.evidence_citations,
            # Persist export join keys, but never canonical question text.
            student_answer=attempt["chosen_letter"] or "",
            correct_answer=row["correct_letter"] or "",
            confidence=ex.confidence,
            generator_notes={"confidence": ex.confidence, "actor": "cli", "question_id": qid},
        )
    print(
        json.dumps(
            {
                "review_id": rev.id,
                "question_id": qid,
                "state": rev.state,
                "mode": rev.generator,
                "model": rev.model,
                "tested_task": rev.tested_task,
                "stale": rev.stale,
            },
            indent=2,
        )
    )


def cmd_review_show(args) -> None:
    """Show the full review body an operator needs before changing its state."""
    with db_context() as conn:
        ensure_review_schema(conn)
        try:
            rev = get_review(conn, args.id)
        except KeyError as e:
            raise SystemExit(str(e)) from e
    payload = {
        "id": rev.id,
        "question_id": rev.question_id,
        "question_fingerprint": rev.question_fingerprint,
        "state": rev.state,
        "stale": rev.stale,
        "generator": rev.generator,
        "model": rev.model,
        "tested_task": rev.tested_task,
        "tempting_answer": rev.tempting_answer,
        "exact_failure": rev.exact_failure,
        "correct_reasoning": rev.correct_reasoning,
        "kb_tactic_refs": rev.kb_tactic_refs,
        "error_taxonomy": rev.error_taxonomy,
        "evidence": json.loads(rev.evidence_json or "[]"),
        "provenance": json.loads(rev.provenance_json or "{}"),
        "student_answer": rev.student_answer,
        "correct_answer": rev.correct_answer,
        "confidence": rev.confidence,
        "created_at": rev.created_at,
        "updated_at": rev.updated_at,
        "approved_at": rev.approved_at,
        "exported_at": rev.exported_at,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_review_list(args) -> None:
    with db_context() as conn:
        ensure_review_schema(conn)
        # The queue surfaces reviews orphaned by corpus changes.
        include_stale = True if args.include_stale else not args.exclude_stale
        rows = list_reviews(conn, state=args.state, include_stale=include_stale)
    print(
        json.dumps(
            [
                {
                    "id": r.id,
                    "question_id": r.question_id,
                    "state": r.state,
                    "generator": r.generator,
                    "model": r.model,
                    "tested_task": r.tested_task,
                    "stale": r.stale,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ],
            indent=2,
        )
    )


def cmd_review_approve(args) -> None:
    with db_context() as conn:
        ensure_review_schema(conn)
        rev = transition(conn, args.id, APPROVED, reason=args.reason, actor="cli")
    print(
        json.dumps(
            {"review_id": rev.id, "state": rev.state, "approved_at": rev.approved_at}, indent=2
        )
    )


def cmd_review_reject(args) -> None:
    with db_context() as conn:
        ensure_review_schema(conn)
        rev = transition(conn, args.id, REJECTED, reason=args.reason, actor="cli")
    print(json.dumps({"review_id": rev.id, "state": rev.state}, indent=2))


def cmd_review_edit(args) -> None:
    with db_context() as conn:
        ensure_review_schema(conn)
        rev = edit_review(
            conn,
            args.id,
            exact_failure=args.exact_failure,
            correct_reasoning=args.correct_reasoning,
            tempting_answer=args.tempting_answer,
            actor="cli",
            reason=args.reason,
        )
    print(
        json.dumps(
            {"review_id": rev.id, "state": rev.state, "exact_failure": rev.exact_failure}, indent=2
        )
    )


def cmd_review_export(args) -> None:
    vault = config.REPO_ROOT / "kb" / "wiki" / "reviews"
    with db_context() as conn:
        ensure_review_schema(conn)
        target = export_review(conn, args.id, vault_dir=vault, dry_run=args.dry_run)
    print(json.dumps({"path": str(target), "dry_run": args.dry_run}, indent=2))


def cmd_review_delete(args) -> None:
    with db_context() as conn:
        ensure_review_schema(conn)
        # Tombstones retain who deleted a review and why.
        try:
            delete_review(conn, args.id, actor="cli", reason=args.reason or "")
        except ReviewStateError as e:
            raise SystemExit(str(e)) from e
    print(json.dumps({"deleted": True, "review_id": args.id}, indent=2))


def cmd_review_deletions(args) -> None:
    """Print audit tombstones for review deletions."""
    with db_context() as conn:
        ensure_review_schema(conn)
        rows = list_deletions(conn, question_id=args.question_id)
    print(json.dumps(rows, indent=2))


def cmd_remediate(args) -> None:
    """Build and print a personalized remediation plan (issue #38)."""
    with db_context() as conn:
        plan = build_remediation_plan(
            conn,
            count=args.count,
            seed=args.seed,
            min_score=args.min_score,
            focus_tag=args.focus_tag,
        )
    print(
        json.dumps(
            {
                "recommended_tags": plan.recommended_tags,
                "patterns": [
                    {
                        "tag": p.tag,
                        "score": p.score,
                        "status": p.status,
                        "evidence_count": p.evidence_count,
                        "recent_wrong": p.recent_wrong,
                        "recent_total": p.recent_total,
                        "kb_tactic_refs": p.kb_tactic_refs,
                    }
                    for p in plan.patterns
                ],
                "improvement": plan.improvement,
                "drill": plan.drill,
                "why": explain_selection(plan),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def cmd_stats(args) -> None:
    with db_context() as conn:
        d = full_dashboard(conn)
    print(json.dumps(d["corpus"], indent=2))
    print("\ntop skill risks:")
    for s in d["skills"][:8]:
        print(f"  {s['risk_score']:5.1f}  {s['skill']:<32} {s['correct']}/{s['seen']}")
    print("\ntop reasoning-tag risks:")
    for t in d["tags"][:10]:
        print(f"  {t['risk_score']:5.1f}  {t['tag']:<36} {t['correct']}/{t['seen']}")
    print("\ntransfer:")
    print(json.dumps(d["transfer"], indent=2))
    if d.get("recent_trend"):
        print("\nrecent trend by tag:")
        for t in d["recent_trend"]:
            print(f"  {t['tag']:<36} {t['recent_accuracy']:>5}%  (n={t['recent_n']})")


#: Hostnames that resolve to this machine only. Anything else is reachable by
#: other hosts, and the UI has no authentication of any kind.
LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def normalize_host(host: str) -> str:
    """The form to hand a socket.

    `[::1]` is URI syntax: brackets disambiguate the address from the port in
    a URL, and `getaddrinfo` rejects them. Accepting the bracketed form while
    passing it through unchanged would mean the server refuses to start on
    exactly the spelling most likely to be copied out of a browser.
    """
    return (host or "").strip().strip("[]")


def is_loopback(host: str) -> bool:
    """True when binding to `host` keeps the UI on this machine.

    Addresses are decided by `ipaddress`, so 127.0.0.1, 127.0.0.53 and ::1 are
    all recognised while 0.0.0.0 (every interface) and :: are not. A name that
    is not a known loopback alias is assumed to be routable: guessing wrong in
    that direction only prints a warning, guessing wrong the other way stays
    silent about an exposed server.
    """
    host = normalize_host(host).lower()
    if not host:
        return False  # uvicorn's own default is every interface
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in LOOPBACK_NAMES


def exposure_notice(host: str, port: int) -> str:
    """What the operator needs to know before this listens off-machine."""
    return (
        f"satprep is listening on {host}:{port}, reachable from other machines.\n"
        "There is no login: anyone who can reach this address can drill, and can\n"
        "read /admin and /review. Intended for a trusted home network only."
    )


def cmd_serve(args) -> None:
    import uvicorn

    host = normalize_host(args.host)
    if not is_loopback(host):
        print(exposure_notice(host, args.port), file=sys.stderr)
    uvicorn.run(
        "satprep.server:app",
        host=host,
        port=args.port,
        reload=False,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="satprep", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="rebuild corpus from raw sources (idempotent)")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser(
        "repair-bluebook",
        help="reconcile historical Bluebook rows from snapshots (issue #49; idempotent)",
    )
    sp.set_defaults(func=cmd_repair_bluebook)

    sp = sub.add_parser(
        "audit-bluebook",
        help="audit Bluebook history against acceptance gates (issue #49; read-only)",
    )
    sp.set_defaults(func=cmd_audit_bluebook)

    sp = sub.add_parser("analyze", help="compute weakness profile")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("drill", help="run a drill in the terminal")
    sp.add_argument("--count", type=int, default=config.DEFAULT_DRILL_SIZE)
    sp.add_argument(
        "--mode",
        default="targeted_drill",
        choices=["targeted_drill", "error_clinic", "transfer_drill", "hard_mixed"],
    )
    sp.add_argument("--focus", default=None, help="focus reasoning tag")
    sp.add_argument("--seed", default=None)
    sp.set_defaults(func=cmd_drill)

    sp = sub.add_parser("benchmark", help="protected fresh-question benchmark")
    sp.add_argument("--count", type=int, default=8)
    sp.add_argument("--seed", default=None)
    sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser(
        "fetch-qbank", help="ingest official College Board Educator Question Bank (public API)"
    )
    sp.add_argument("--hard-only", action="store_true", help="only import items CB marks Hard")
    sp.add_argument("--domains", default="", help="comma list, e.g. INI,CAS (default all R&W)")
    sp.add_argument("--limit", type=int, default=0, help="cap number fetched (0 = all)")
    sp.add_argument("--sleep", type=float, default=0.25, help="politeness delay seconds")
    sp.add_argument(
        "--full-sweep",
        action="store_true",
        help="backfill visuals on ALL imageless bank rows, not just stems that name a figure/table",
    )
    sp.add_argument(
        "--audit-visuals",
        action="store_true",
        help="run the visual backfill sweep read-only and report "
        "what would change (issue #46 audit); no rows written",
    )
    sp.set_defaults(func=cmd_fetch_qbank)

    sp = sub.add_parser("export", help="write the JSONL corpus archive")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("explain", help="KB-aware error explanation for a question")
    sp.add_argument("--question-id", type=int, required=True, help="questions.id to explain")
    sp.add_argument(
        "--student-letter",
        default=None,
        help="override the student's chosen letter; defaults "
        "to the most recent wrong attempt on this question",
    )
    sp.set_defaults(func=cmd_explain)

    rv = sub.add_parser("review", help="manage student question reviews")
    rv_sub = rv.add_subparsers(dest="review_cmd", required=True)

    rsp = rv_sub.add_parser(
        "generate", help="generate a draft review from the most recent wrong attempt on a question"
    )
    rsp.add_argument("--question-id", type=int, required=True)
    rsp.set_defaults(func=cmd_review_generate)

    rsp = rv_sub.add_parser(
        "show", help="show one review's full body (diagnosis + provenance + evidence refs)"
    )
    rsp.add_argument("--id", type=int, required=True)
    rsp.set_defaults(func=cmd_review_show)

    rsp = rv_sub.add_parser("list", help="list reviews (draft/approved/... )")
    rsp.add_argument("--state", default=None, choices=["draft", "approved", "rejected", "edited"])
    # Stale rows are visible unless explicitly excluded.
    rsp.add_argument(
        "--include-stale",
        action="store_true",
        default=True,
        help="(default) include stale reviews in the list",
    )
    rsp.add_argument(
        "--exclude-stale", action="store_true", help="omit stale reviews from the list"
    )
    rsp.set_defaults(func=cmd_review_list)

    rsp = rv_sub.add_parser(
        "approve", help="approve a draft review (explicit human step before export)"
    )
    rsp.add_argument("--id", type=int, required=True)
    rsp.add_argument("--reason", default="")
    rsp.set_defaults(func=cmd_review_approve)

    rsp = rv_sub.add_parser("reject", help="reject a review (terminal)")
    rsp.add_argument("--id", type=int, required=True)
    rsp.add_argument("--reason", default="")
    rsp.set_defaults(func=cmd_review_reject)

    rsp = rv_sub.add_parser("edit", help="edit an approved review's fields")
    rsp.add_argument("--id", type=int, required=True)
    rsp.add_argument("--exact-failure", default="")
    rsp.add_argument("--correct-reasoning", default="")
    rsp.add_argument("--tempting-answer", default="")
    rsp.add_argument("--reason", default="")
    rsp.set_defaults(func=cmd_review_edit)

    rsp = rv_sub.add_parser(
        "export", help="write an approved review to the versioned Markdown vault (kb/wiki/reviews/)"
    )
    rsp.add_argument("--id", type=int, required=True)
    rsp.add_argument("--dry-run", action="store_true", help="print the target path without writing")
    rsp.set_defaults(func=cmd_review_export)

    rsp = rv_sub.add_parser("delete", help="delete a draft/rejected review")
    rsp.add_argument("--id", type=int, required=True)
    rsp.add_argument("--reason", default="", help="audit reason (recorded on the tombstone)")
    rsp.set_defaults(func=cmd_review_delete)

    rsp = rv_sub.add_parser("deletions", help="list review-deletion tombstones (PR-44 audit log)")
    rsp.add_argument("--question-id", type=int, default=None, help="filter to a single question_id")
    rsp.set_defaults(func=cmd_review_deletions)

    sp = sub.add_parser("restore", help="rebuild questions from a JSONL archive")
    sp.add_argument("--file", default=None)
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("stats", help="print dashboard statistics")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser(
        "remediate", help="build a personalized remediation plan from error patterns (issue #38)"
    )
    sp.add_argument("--count", type=int, default=12)
    sp.add_argument("--seed", default=None)
    sp.add_argument(
        "--min-score", type=float, default=40.0, help="remediation priority floor (0-100)"
    )
    sp.add_argument("--focus-tag", default=None, help="focus remediation on one error tag")
    sp.set_defaults(func=cmd_remediate)

    sp = sub.add_parser("serve", help="start the web UI")
    sp.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind (default 127.0.0.1, this machine only; "
        "use 0.0.0.0 to serve the local network)",
    )
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
