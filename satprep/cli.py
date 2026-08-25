"""satprep command line interface.

    satprep ingest              # rebuild corpus from outputs/ + imports/
    satprep analyze             # (re)compute tags + weakness profile
    satprep drill [--count N] [--focus TAG] [--mode MODE]
    satprep benchmark           # protected fresh benchmark
    satprep stats               # dashboard numbers in terminal
    satprep serve [--port P]    # local web UI
"""

import argparse
import json
import sys
from datetime import datetime

from . import config


def cmd_ingest(args) -> None:
    from .ingest import ingest_bluebook, ingest_qbank
    from .tagger import run_full_tagging

    b = ingest_bluebook()
    print(f"bluebook history: {b}")
    q = ingest_qbank()
    print(f"question bank imports: {q}")
    t = run_full_tagging()
    print(f"tagging: {t}")


def cmd_analyze(args) -> None:
    from .weakness import compute_weakness

    scores = compute_weakness()
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
    from .sessions import complete_session, create_session, review_payload, submit_answer

    mode = args.mode or "targeted_drill"
    sess = create_session(mode=mode, count=args.count, seed=args.seed,
                          focus_tag=args.focus)
    plan, questions = sess["plan"], sess["questions"]
    if not questions:
        print("No questions available for this mode. Run 'satprep ingest' first.")
        return
    sid = plan["session_id"]
    start = datetime.now().astimezone()
    print(f"\n== {mode} | session {sid} | {len(questions)} questions ==\n")
    for i, q in enumerate(questions, 1):
        print(f"--- Question {i}/{len(questions)} ---")
        if q["passage"]:
            print(q["passage"][:1800])
            print()
        print(q["stem"])
        for c in sorted(q["choices"], key=lambda c: c["letter"]):
            print(f"  {c['letter']}. {c['text'][:300]}")
        letter, conf = _interactive_answer()
        ms = int((datetime.now().astimezone() - start).total_seconds() * 1000 / max(1, i))
        res = submit_answer(sid, q["id"], letter, conf, ms)
        print(("correct" if res["correct"] else f"wrong (key: {res['key']})") + "\n")
    summary = complete_session(sid)
    print("summary:", json.dumps(summary))
    reviews = [r for r in review_payload(sid) if not r["correct"]]
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
    from .qbank_fetch import fetch_qbank

    domains = [d.strip().upper() for d in args.domains.split(",") if d.strip()] or None
    stats = fetch_qbank(hard_only=args.hard_only, domains=domains,
                        limit=args.limit, sleep_s=args.sleep)
    print("done:", json.dumps(stats))


def cmd_stats(args) -> None:
    from .analytics import full_dashboard

    d = full_dashboard()
    print(json.dumps(d["corpus"], indent=2))
    print("\ntop skill risks:")
    for s in d["skills"][:8]:
        print(f"  {s['risk_score']:5.1f}  {s['skill']:<32} {s['correct']}/{s['seen']}")
    print("\ntop reasoning-tag risks:")
    for t in d["tags"][:10]:
        print(f"  {t['risk_score']:5.1f}  {t['tag']:<36} {t['correct']}/{t['seen']}")
    print("\ntransfer:")
    print(json.dumps(d["transfer"], indent=2))


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run(
        "satprep.server:app",
        host="127.0.0.1",
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

    sp = sub.add_parser("stats", help="print dashboard statistics")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("serve", help="start the local web UI")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
