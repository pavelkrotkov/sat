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
import sys
from datetime import datetime

from . import config
from .db import db_context
from .analytics import full_dashboard
from .corpus.archive import ARCHIVE_VERSION, export_corpus, restore_corpus
from .corpus.ingest import ingest_bluebook, ingest_qbank
from .corpus.qbank_fetch import fetch_qbank
from .corpus.tagger import run_full_tagging
from .training.sessions import (complete_session, create_session, review_payload,
                                submit_answer)
from .training.weakness import compute_weakness


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
    archive = (pathlib.Path(args.file) if args.file
               else config.REPO_ROOT / "exports" / f"corpus-v{ARCHIVE_VERSION}.jsonl")
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


def _run_drill(conn, mode: str, args) -> None:
    sess = create_session(conn, mode=mode, count=args.count, seed=args.seed,
                          focus_tag=args.focus)
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
        print(f"\nREVIEW Q{r['question_id']} [{r['official_skill']}] trap={','.join(r['trap_tags'])}")
        if r["passage_skeleton"]:
            print("skeleton:", " | ".join(s[:120] for s in r["passage_skeleton"]))
        if r["rationale_official"]:
            print("rationale:", r["rationale_official"][:500])


def cmd_benchmark(args) -> None:
    args.mode = "fresh_benchmark"
    if not args.count:
        args.count = 8
    cmd_drill(args)


def cmd_fetch_qbank(args) -> None:
    domains = [d.strip().upper() for d in args.domains.split(",") if d.strip()] or None
    with db_context() as conn:
        stats = fetch_qbank(conn, hard_only=args.hard_only, domains=domains,
                            limit=args.limit, sleep_s=args.sleep)
        print("done:", json.dumps(stats))
        # tag BEFORE snapshotting so the archive never stores tag-less rows
        print(f"tagging: {run_full_tagging(conn)}")
        _auto_export(conn)


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
        return False       # uvicorn's own default is every interface
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
    p = argparse.ArgumentParser(prog="satprep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="rebuild corpus from raw sources (idempotent)")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("analyze", help="compute weakness profile")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("drill", help="run a drill in the terminal")
    sp.add_argument("--count", type=int, default=config.DEFAULT_DRILL_SIZE)
    sp.add_argument("--mode", default="targeted_drill",
                    choices=["targeted_drill", "error_clinic", "transfer_drill", "hard_mixed"])
    sp.add_argument("--focus", default=None, help="focus reasoning tag")
    sp.add_argument("--seed", default=None)
    sp.set_defaults(func=cmd_drill)

    sp = sub.add_parser("benchmark", help="protected fresh-question benchmark")
    sp.add_argument("--count", type=int, default=8)
    sp.add_argument("--seed", default=None)
    sp.set_defaults(func=cmd_benchmark)

    sp = sub.add_parser("fetch-qbank",
                        help="ingest official College Board Educator Question Bank (public API)")
    sp.add_argument("--hard-only", action="store_true",
                    help="only import items CB marks Hard")
    sp.add_argument("--domains", default="", help="comma list, e.g. INI,CAS (default all R&W)")
    sp.add_argument("--limit", type=int, default=0, help="cap number fetched (0 = all)")
    sp.add_argument("--sleep", type=float, default=0.25, help="politeness delay seconds")
    sp.set_defaults(func=cmd_fetch_qbank)

    sp = sub.add_parser("export", help="write the JSONL corpus archive")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("restore", help="rebuild questions from a JSONL archive")
    sp.add_argument("--file", default=None)
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("stats", help="print dashboard statistics")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("serve", help="start the web UI")
    sp.add_argument("--host", default="127.0.0.1",
                    help="interface to bind (default 127.0.0.1, this machine only; "
                         "use 0.0.0.0 to serve the local network)")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
