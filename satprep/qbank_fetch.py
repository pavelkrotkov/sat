"""Automated ingestion from the public College Board Educator Question Bank.

The EQB web app (satsuiteeducatorquestionbank.collegeboard.org) is public
(no login required) and is backed by an undocumented-but-stable JSON API.
This module uses it read-only, politely, for the private local training
corpus only - never for redistribution (spec section 22).

Endpoints used:
  POST .../questionbank/lookup            -> taxonomy (domains/skills)
  POST .../digital/get-questions          -> metadata search
        {asmtEventId: 99(SAT), test: 1(R&W), domain: "INI,CAS,..."}
  POST .../digital/get-question           -> full content {external_id}
"""

import json
import re
import time
import urllib.error
import urllib.request

from . import config
from .db import connect

BASE = "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank"
SAT_RW_ASMT = 99   # SAT
SAT_RW_TEST = 1    # Reading and Writing
DOMAINS = ["INI", "CAS", "EOI", "SEC"]

_DIFFICULTY_MAP = {"H": "hard", "M": "medium", "L": "easy", "E": "easy"}

_STRIPTAGS = re.compile(r"<[^>]+>")


def _post(url: str, payload: dict, retries: int = 3) -> object:
    body = json.dumps(payload).encode()
    last_exc = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"EQB request failed after {retries} tries: {url}: {last_exc}")


def _clean_html(html: str | None) -> str:
    if not html:
        return ""
    import html as html_mod

    text = _STRIPTAGS.sub(" ", html)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def list_questions(domains: list[str] | None = None,
                   assessment: int = SAT_RW_ASMT, test: int = SAT_RW_TEST) -> list[dict]:
    out: list[dict] = []
    for cd in (domains or DOMAINS):
        rows = _post(
            f"{BASE}/digital/get-questions",
            {"asmtEventId": assessment, "test": test, "domain": cd},
        )
        if isinstance(rows, list):
            out.extend(rows)
    return out


def fetch_question(external_id: str) -> dict | None:
    try:
        return _post(f"{BASE}/digital/get-question", {"external_id": external_id})
    except RuntimeError:
        return None


def _normalize(detail: dict, meta: dict) -> dict | None:
    options = detail.get("answerOptions") or []
    if len(options) < 2:
        return None
    letters = [chr(ord("A") + i) for i in range(len(options))]
    correct_letter = ""
    ca = detail.get("correct_answer")
    if isinstance(ca, list) and ca:
        correct_letter = str(ca[0]).strip().upper()[:1]
    choices = [
        {"letter": letters[i], "text": _clean_html(o.get("content")), "is_correct": False}
        for i, o in enumerate(options)
    ]
    if not correct_letter:
        keys = set(detail.get("keys") or [])
        for i, o in enumerate(options):
            if o.get("id") in keys:
                correct_letter = letters[i]
                break
    if not correct_letter:
        return None
    for c in choices:
        c["is_correct"] = c["letter"] == correct_letter
    difficulty = meta.get("difficulty") or ""
    difficulty = _DIFFICULTY_MAP.get(str(difficulty).strip().upper(),
                                     str(difficulty).strip().lower())
    return {
        "passage": _clean_html(detail.get("stimulus")),
        "stem": _clean_html(detail.get("stem")),
        "choices": choices,
        "correct": correct_letter,
        "domain": meta.get("primary_class_cd_desc", ""),
        "skill": meta.get("skill_desc", ""),
        "difficulty": difficulty if difficulty in ("easy", "medium", "hard") else "",
        "rationale": _clean_html(detail.get("rationale")),
        "ext_id": str(meta.get("external_id") or detail.get("externalid") or ""),
    }


def insert_qbank_row(conn, row: dict, batch: str) -> str:
    """Shared insertion path for file imports and live fetches.

    Returns one of: 'added', 'duplicate', 'invalid'.
    """
    from . import fingerprint as fpmod
    from .ingest import utc_now

    choices = row["choices"]
    correct = row["correct"]
    if len(choices) < 2 or not correct:
        return "invalid"
    fp = fpmod.fingerprint(row["passage"], row["stem"], [c["text"] for c in choices])
    exists = conn.execute("SELECT id FROM questions WHERE fingerprint=?", (fp,)).fetchone()
    if exists:
        return "duplicate"
    pool = fpmod.pool_for_fingerprint(fp)
    diff = (row.get("difficulty") or "").strip().lower()
    conn.execute(
        """INSERT INTO questions
           (fingerprint, source, source_test, source_question_number, module,
            passage, stem, choices_json, correct_letter, rationale, images_json,
            official_domain, official_skill, skill_source, difficulty,
            pool, seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)""",
        (
            fp, "college_board_question_bank", batch, row.get("ext_id", ""), "",
            row["passage"], row["stem"], json.dumps(choices), correct,
            row.get("rationale", ""), "[]",
            row.get("domain", ""), row.get("skill", ""),
            "metadata" if row.get("skill") else "unknown",
            diff if diff in ("easy", "medium", "hard") else "",
            pool, batch, utc_now(), json.dumps({"external_id": row.get("ext_id", "")}),
        ),
    )
    qid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO question_state (question_id) VALUES (?)", (qid,))
    return "added"


def known_external_ids(conn) -> set[str]:
    ids = set()
    for r in conn.execute("SELECT source_question_number, provenance_json FROM questions"):
        try:
            ext = json.loads(r["provenance_json"] or "{}").get("external_id")
        except json.JSONDecodeError:
            ext = None
        if ext:
            ids.add(ext)
        if r["source_question_number"]:
            ids.add(r["source_question_number"])
    return ids


def fetch_qbank(hard_only: bool = False, domains: list[str] | None = None,
                limit: int = 0, sleep_s: float = 0.25, db_path=None) -> dict:
    """Pull SAT R&W items from the public EQB into the corpus. Idempotent."""
    from datetime import datetime, timezone

    conn = connect(db_path)
    have = known_external_ids(conn)
    batch = f"eqb-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    stats = {"listed": 0, "skipped_known": 0, "fetched": 0,
             "added": 0, "duplicates": 0, "invalid": 0, "failed": 0}

    metas = list_questions(domains)
    stats["listed"] = len(metas)
    todo = []
    for m in metas:
        ext = str(m.get("external_id") or "")
        if not ext or ext in have:
            stats["skipped_known"] += 1
            continue
        if hard_only and str(m.get("difficulty", "")).upper() != "H":
            continue
        todo.append(m)
    if limit:
        todo = todo[:limit]

    print(f"eqb: {stats['listed']} listed, {len(todo)} to fetch "
          f"(batch {batch}, hard_only={hard_only})")
    for i, m in enumerate(todo, 1):
        detail = fetch_question(str(m["external_id"]))
        if detail is None:
            stats["failed"] += 1
            continue
        stats["fetched"] += 1
        row = _normalize(detail, m)
        if row is None:
            stats["invalid"] += 1
            continue
        outcome = insert_qbank_row(conn, row, batch)
        stats[outcome] = stats.get(outcome, 0) + 1
        if i % 50 == 0:
            conn.commit()
            print(f"  {i}/{len(todo)} … {stats}")
        time.sleep(sleep_s)
    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    print(fetch_qbank())
