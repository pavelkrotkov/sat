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

import base64
import html as html_mod
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .. import config
from ..config import SKILL_TO_DOMAIN
from ..clock import utc_now
from . import fingerprint as fpmod

BASE = "https://qbank-api.collegeboard.org/msreportingquestionbank-prod/questionbank"
SAT_RW_ASMT = 99   # SAT
SAT_RW_TEST = 1    # Reading and Writing
DOMAINS = ["INI", "CAS", "EOI", "SEC"]

# Inline figures (EQB embeds graphs/diagrams in the stimulus HTML) are written
# here; the server serves this directory at /figures. Overridable for tests.
FIGURE_DIR = config.IMAGES_DIR

_DIFFICULTY_MAP = {"H": "hard", "M": "medium", "L": "easy", "E": "easy"}

_STRIPTAGS = re.compile(r"<[^>]+>")
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
_IMG_DATA_RE = re.compile(
    r"<img\b[^>]*?\bsrc=(['\"])(data:image/(?:png|jpe?g|gif|webp|svg\+xml);base64,[^'\"]+)\1",
    re.IGNORECASE | re.DOTALL,
)
# One document-order pass over all visual shapes. Group 1 holds the
# data-URI payload, set only by the <img> branch (an <img> inside a <figure>
# is consumed by the figure branch first, at the earlier offset).
_ANY_FIGURE_RE = re.compile(
    r"<figure\b.*?</figure>"
    r"|<svg\b.*?</svg>"
    r"|<table\b.*?</table>"
    r"|<img\b[^>]*?\bsrc=(?:['\"])("
    r"data:image/(?:png|jpe?g|gif|webp|svg\+xml);base64,[^'\"]+)(?:['\"][^>]*)?>",
    re.IGNORECASE | re.DOTALL,
)

#: Sanitized visual records look like {'kind': 'table', 'html': '<table …>'}
#: or {'kind': 'image', 'file': 'eqb-…-figure-1.svg'}. The template renders
#: table html with |safe — sanitization here is the ONLY reason that is
#: acceptable, so the output must be structurally limited to the allowlist.
_TABLE_ALLOWED_TAGS = {
    "table", "caption", "thead", "tbody", "tfoot",
    "tr", "th", "td", "col", "colgroup",
}
_TABLE_ALLOWED_ATTRS = {
    "th": {"scope"},
    "td": {"colspan", "rowspan", "headers"},
    "col": {"span"},
}


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
    text = _STRIPTAGS.sub(" ", html)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _save_figure(ext_id: str, index: int, content: bytes, suffix: str,
                 image_dir: Path) -> str:
    """Write one figure into image_dir; returns the bare filename stored in
    images_json (the template renders /figures/<name> via the basename filter)."""
    image_dir.mkdir(parents=True, exist_ok=True)
    name = f"eqb-{ext_id}-figure-{index}.{suffix}"
    (image_dir / name).write_bytes(content)
    return name


def sanitize_table(html: str) -> str | None:
    """Reduce arbitrary `<table>` markup to a safe, accessible subset.

    Issue #46: the EQB embeds data tables as real `<figure class="table">`
    HTML. Persisting them as first-class visuals needs a representation that
    is safe to render with `|safe` in Jinja — the allowlist is the whole
    point: only table-structure tags survive, every attribute except the
    layout ones below is dropped, and no script/style/event/URL content
    (or nested markup) can ride along.

    Returns a normalized `<table>…</table>` string with cells' visible text,
    or None when the markup contains no table at all.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return None
    # Rewrite header ids so any headers="..." reference stays within the
    # table we persist (source ids could collide with page ids or be
    # absent). The id counter makes the map deterministic per table, and the
    # same map remaps every headers="..." token so references never dangle.
    id_map: dict[str, str] = {}
    for i, th in enumerate(table.find_all("th"), 1):
        src = th.get("id")
        if isinstance(src, str) and src:
            id_map[src] = f"eqb-th-{i}"
    for tag in table.find_all(True):
        if tag.name not in _TABLE_ALLOWED_TAGS:
            tag.decompose()
            continue
        for attr in list(tag.attrs):
            raw = tag.get(attr)
            # BeautifulSoup returns multi-valued attrs (class, headers, …)
            # as lists; coerce to the space-joined string we validate.
            if raw is None:
                value = ""
            elif isinstance(raw, str):
                value = raw
            else:
                value = " ".join(str(part) for part in raw if part is not None)
            if tag.name == "th" and attr == "id" and value in id_map:
                tag["id"] = id_map[value]
                continue
            if attr == "headers":
                # tokens are space-separated header ids; remap the ones we
                # know, drop the rest (they can only dangle after the purge)
                tokens = [id_map[t] for t in value.split() if t in id_map]
                if tokens:
                    tag["headers"] = " ".join(tokens)
                else:
                    del tag["headers"]
                continue
            if attr not in _TABLE_ALLOWED_ATTRS.get(tag.name, set()):
                del tag[attr]
                continue
            if not re.fullmatch(r"[0-9a-zA-Z\s]+", value or ""):
                del tag[attr]
    # Tables whose cells carry no text (or whose structure collapsed to
    # nothing) are useless to a student; skip rather than persist a husk.
    if not any((tag.get_text(strip=True)) for tag in table.find_all(["th", "td"])):
        return None
    return str(table)


def _extract_visuals(ext_id: str, html: str, image_dir: Path,
                     start_index: int = 1) -> tuple[str, list[dict], int]:
    """Pull inline visuals (figures AND data tables) out of EQB HTML.

    Returns (remaining_html, visual_records, next_index). One
    document-order pass over <figure> blocks, bare inline <svg>, data-URI
    <img> tags, and <table> blocks.

    - Figures (svg/data-URI) are saved to image_dir; their record is
      {'kind': 'image', 'file': <bare filename>} and the template renders
      /figures/<file>.
    - Tables are sanitized via `sanitize_table`; their record is
      {'kind': 'table', 'html': '<table …>'}. The stored text fallback
      (caption + cell text flattened, below) keeps the cleaned
      passage/stem/fingerprint identical to what the old ingest stored.

    The matched markup is replaced by its visible text (SVG <text> nodes,
    <figcaption>, alt text, and for tables the caption + cell text) so that
    axis labels and captions still reach the cleaned passage/stem — which
    keeps legacy fingerprints (computed from the fully stripped text) stable
    across re-imports. `start_index`/`next_index` let callers share one
    numbering across multiple HTML fields (stem + stimulus) so filenames stay
    unique in document order.
    """
    visuals: list[dict] = []
    index = start_index

    def _payload(match: re.Match) -> tuple[str, bytes, str] | None:
        """(kind, content, suffix) for one match, or None to skip (keep text)."""
        block = match.group(0)
        data = match.group(1)  # set only by the bare data-URI <img> branch
        if data is None:
            svg = _SVG_RE.search(block)
            if svg:
                return "image", svg.group(0).encode("utf-8"), "svg"
            inner = _IMG_DATA_RE.search(block)  # <figure><img data:...></figure>
            if inner:
                data = inner.group(2)
            else:
                # College Board renders data tables as <figure class="table">
                # wrapping a real <table>; the <figure> regex branch consumes
                # the whole wrapper, so the table must be found INSIDE it
                # (a bare <table> is matched by its own regex alternative and
                # arrives here through the same search).
                table = _TABLE_RE.search(html)
                if table:
                    sanitized = sanitize_table(table.group(0))
                    if sanitized is None:
                        return None
                    return "table", sanitized.encode("utf-8"), "html"
                return None  # <figure> with no savable asset: keep its text
        if "," not in data:  # malformed data URI: skip, keep as text
            return None
        # Long payloads commonly serialize with newlines every ~76 chars;
        # b64decode(validate=True) raises on any whitespace, which silently
        # dropped the figure (issue #31). Strip before decoding.
        b64 = re.sub(r"\s+", "", data.split(",", 1)[1])
        if not b64:  # whitespace-only payload: nothing to save, skip
            return None
        mime = data[len("data:image/"):].split(";", 1)[0].lower()
        suffix = {"jpeg": "jpg", "svg+xml": "svg"}.get(mime) or mime
        suffix = suffix.rsplit("/", 1)[-1].split("+", 1)[-1]
        if not suffix.isalnum() or len(suffix) > 5:
            return None  # unknown/unsafe type: skip
        try:
            return "image", base64.b64decode(b64, validate=True), suffix
        except Exception:
            return None  # malformed payload: skip, keep as text

    def _repl(match: re.Match) -> str:
        nonlocal index
        got = _payload(match)
        if got is not None:
            kind, content, suffix = got
            if kind == "table":
                visuals.append({"kind": "table", "html": content.decode("utf-8")})
            else:
                name = _save_figure(ext_id, index, content, suffix, image_dir)
                visuals.append({"kind": "image", "file": name})
            index += 1
        # always leave the visible text (labels, captions, alt) in place
        return _clean_html(match.group(0)) or " "

    remaining = _ANY_FIGURE_RE.sub(_repl, html)
    return remaining, visuals, index


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


def _normalize(detail: dict, meta: dict, image_dir: Path | None = None) -> dict | None:
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
    # Visuals (graphs/diagrams/tables) live inline in the stem and stimulus
    # HTML. Save them before _clean_html strips markup; their visible text
    # (labels, captions, cell text) is left in place so the stored text still
    # matches legacy rows.
    image_dir = image_dir if image_dir is not None else FIGURE_DIR
    ext_id = str(meta.get("external_id") or detail.get("externalid") or "")
    # One numbering across both fields so stem+stimulus visuals never
    # collide on a filename, in document order within each field.
    stem_html, stem_visuals, nxt = _extract_visuals(ext_id, detail.get("stem") or "", image_dir)
    stim_html, stim_visuals, _ = _extract_visuals(ext_id, detail.get("stimulus") or "",
                                                  image_dir, start_index=nxt)
    visuals = stem_visuals + stim_visuals
    images = [v["file"] for v in visuals if v.get("kind") == "image"]
    return {
        "passage": _clean_html(stim_html),
        "stem": _clean_html(stem_html),
        "choices": choices,
        "images": images,
        "visuals": visuals,
        "correct": correct_letter,
        "domain": meta.get("primary_class_cd_desc", ""),
        "skill": meta.get("skill_desc", ""),
        "difficulty": difficulty if difficulty in ("easy", "medium", "hard") else "",
        "rationale": _clean_html(detail.get("rationale")),
        "ext_id": ext_id,
    }


def insert_qbank_row(conn, row: dict, batch: str) -> str:
    """Shared insertion path for file imports and live fetches.

    Returns one of: 'added', 'duplicate', 'invalid'.
    """
    choices = row["choices"]
    correct = row["correct"]
    if len(choices) < 2 or not correct:
        return "invalid"
    fp = fpmod.fingerprint(row["passage"], row["stem"], [c["text"] for c in choices])
    exists = conn.execute(
        "SELECT id, choices_json, images_json, visuals_json, official_skill, difficulty, pool FROM questions WHERE fingerprint=?",
        (fp,),
    ).fetchone()

    images = row.get("images") or []
    images_json = json.dumps(images)
    visuals = row.get("visuals") or []
    visuals_json = json.dumps(visuals)

    def _backfill_images(target_id: int, current: str) -> None:
        if not images:
            return
        try:
            if json.loads(current or "null"):
                return  # already has figures
        except ValueError:
            pass
        conn.execute("UPDATE questions SET images_json=? WHERE id=?",
                     (images_json, target_id))

    def _backfill_visuals(target_id: int, current: str) -> None:
        if not visuals:
            return
        try:
            if json.loads(current or "null"):
                return  # already has visuals
        except ValueError:
            pass
        conn.execute("UPDATE questions SET visuals_json=? WHERE id=?",
                     (visuals_json, target_id))

    def _reconcile(target_id: int) -> str:
        skill = exists_row["official_skill"] or row.get("skill", "")
        domain = SKILL_TO_DOMAIN.get(skill, "") if skill else ""
        diff = (row.get("difficulty") or "").strip().lower()
        conn.execute(
            """UPDATE questions SET choices_json=?, correct_letter=?,
                   difficulty=CASE WHEN ?!='' THEN ? ELSE difficulty END,
                   official_skill=?, official_domain=?,
                   skill_source=CASE WHEN ?!='' THEN 'reconciled' ELSE skill_source END,
                   rationale=CASE WHEN rationale='' THEN ? ELSE rationale END
               WHERE id=?""",  # noqa: E501
            (json.dumps(choices), row["correct"], diff, diff,
             skill, domain,
             exists_row["official_skill"] == "" and bool(skill),
             row.get("rationale", ""), target_id),
        )
        _backfill_images(target_id, exists_row["images_json"] or "")
        _backfill_visuals(target_id, exists_row["visuals_json"] or "")
        return "duplicate"

    exists_row = None
    if exists:
        # Cross-source match (spec section 2): never counted as fresh.
        exists_row = exists
        if exists["choices_json"] == "[]" and choices:
            return _reconcile(exists["id"])
        # full duplicate: still reconcile authoritative metadata the historical
        # scrape lacked (difficulty was never recorded by Bluebook)
        diff = (row.get("difficulty") or "").strip().lower()
        skill = row.get("skill", "")
        conn.execute(
            """UPDATE questions SET
                  difficulty=CASE WHEN difficulty='' AND ?!='' THEN ? ELSE difficulty END,
                  official_skill=CASE WHEN official_skill='' AND ?!='' THEN ? ELSE official_skill END,
                  official_domain=CASE WHEN official_skill='' AND ?!='' THEN ? ELSE official_domain END,
                  rationale=CASE WHEN rationale='' AND ?!='' THEN ? ELSE rationale END
              WHERE id=?""",
            (diff, diff, skill, skill, skill, SKILL_TO_DOMAIN.get(skill, ""),
             row.get("rationale", ""), row.get("rationale", ""), exists["id"]),
        )
        _backfill_images(exists["id"], exists["images_json"] or "")
        _backfill_visuals(exists["id"], exists["visuals_json"] or "")
        return "duplicate"

    # External-identity pass: the content fingerprint path above already handled
    # identical re-imports. This pass guards against server-side content drift —
    # College Board rephrasing a question between fetches — keyed on the canonical
    # external_id. Matching on external_id means a known item reconciles instead
    # of inserting a fresh duplicate (which could land in the protected
    # benchmark pool).
    ext = row.get("ext_id", "")
    if ext:
        # (no content-fingerprint match — that path returned 'duplicate'
        # above; matching the same row twice here would be a no-op at best)
        ext_row = conn.execute(
            """SELECT id, choices_json, images_json, visuals_json, official_skill, difficulty, pool
              FROM questions
              WHERE active=1
                AND source='college_board_question_bank'
                AND json_valid(provenance_json)
                AND json_extract(provenance_json, '$.external_id')=?
              ORDER BY id DESC""",
            (ext,),
        ).fetchone()
        if ext_row is not None:
            exists_row = ext_row
            if ext_row["choices_json"] == "[]" and choices:
                return _reconcile(ext_row["id"])
            diff = (row.get("difficulty") or "").strip().lower()
            skill = row.get("skill", "")
            conn.execute(
                """UPDATE questions SET
                      difficulty=CASE WHEN difficulty='' AND ?!='' THEN ? ELSE difficulty END,
                      official_skill=CASE WHEN official_skill='' AND ?!='' THEN ? ELSE official_skill END,
                      official_domain=CASE WHEN official_skill='' AND ?!='' THEN ? ELSE official_domain END,
                      rationale=CASE WHEN rationale='' AND ?!='' THEN ? ELSE rationale END
                  WHERE id=?""",
                (diff, diff, skill, skill, skill, SKILL_TO_DOMAIN.get(skill, ""),
                 row.get("rationale", ""), row.get("rationale", ""), ext_row["id"]),
            )
            _backfill_images(ext_row["id"], ext_row["images_json"] or "")
            _backfill_visuals(ext_row["id"], ext_row["visuals_json"] or "")
            return "duplicate"

    # Loose reconciliation pass: a stored choice-less record whose normalized
    # passage+stem matches this bank item IS the same question seen before.
    loose = fpmod.fingerprint_loose(row["passage"], row["stem"])
    for cand in conn.execute(
        """SELECT id, passage, stem, official_skill, choices_json, images_json, visuals_json FROM questions
         WHERE active=1 AND choices_json='[]'"""
    ).fetchall():
        if fpmod.fingerprint_loose(cand["passage"], cand["stem"]) != loose:
            continue
        exists_row = cand
        return _reconcile(cand["id"])
    pool = fpmod.pool_for_fingerprint(fp)
    diff = (row.get("difficulty") or "").strip().lower()
    conn.execute(
        """INSERT INTO questions
          (fingerprint, source, source_test, source_question_number, module,
           passage, stem, choices_json, correct_letter, rationale, images_json,
           visuals_json,
           official_domain, official_skill, skill_source, difficulty,
           pool, seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)""",
        (
            fp, "college_board_question_bank", batch, row.get("ext_id", ""), "",
            row["passage"], row["stem"], json.dumps(choices), correct,
            row.get("rationale", ""), images_json, visuals_json,
            row.get("domain", ""), row.get("skill", ""),
            "metadata" if row.get("skill") else "unknown",
            diff if diff in ("easy", "medium", "hard") else "",
            pool, batch, utc_now(),
            json.dumps(row.get("_provenance") or {"external_id": row.get("ext_id", "")}),
        ),
    )
    qid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO question_state (question_id) VALUES (?)", (qid,))
    return "added"


def known_external_ids(conn) -> set[str]:
    """External_ids already represented by an ACTIVE row.

    Deactivated (active=0) rows are kept as audit history but must not block
    a re-fetch: the live fetch path uses this set as its skip-list, so
    excluding inactive rows lets a deactivated item be re-imported fresh.
    """
    ids = set()
    for r in conn.execute(
        "SELECT source_question_number, provenance_json FROM questions WHERE active=1"
    ):
        try:
            ext = json.loads(r["provenance_json"] or "{}").get("external_id")
        except json.JSONDecodeError:
            ext = None
        if ext:
            ids.add(ext)
        if r["source_question_number"]:
            ids.add(r["source_question_number"])
    return ids


def fetch_qbank(conn, hard_only: bool = False, domains: list[str] | None = None,
                limit: int = 0, sleep_s: float = 0.25) -> dict:
    """Pull SAT R&W items from the public EQB into the corpus. Idempotent."""
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
            # Deliberate checkpoint inside the caller's transaction: this loop
            # spans thousands of network round trips, and the batch is
            # documented as resumable. Losing an hour of fetching to one
            # timeout is a worse failure than a partial batch, which the
            # external_id skip makes harmless on the next run.
            conn.commit()
            print(f"  {i}/{len(todo)} … {stats}")
        time.sleep(sleep_s)
    return stats


def backfill_visuals(conn, hint: bool = True, limit: int = 0,
                     sleep_s: float = 0.25, audit_only: bool = False) -> dict:
    """Re-fetch bank questions whose visuals (figures/tables) were dropped at
    ingest time and attach them.

    The original EQB ingest stripped all markup from stimulus/stem without
    saving the inline visuals, so graph/diagram/table questions stored empty
    images_json and visuals_json. This exists solely to repair those rows:
    matching is by stored external_id, visuals are re-extracted from the live
    record, and only rows that gain visuals are updated.

    `hint` limits the sweep to rows whose stem cites a visual. Returns a stats
    dict; updates are committed incrementally so the sweep is resumable. With
    `audit_only`, no rows are written — the sweep reports what WOULD change,
    which is the acceptance-criteria audit for issue #46.
    """
    sql = (
        "SELECT id, source_question_number, provenance_json, images_json, visuals_json"
        " FROM questions"
        " WHERE source='college_board_question_bank' AND active=1"
        " AND (images_json IN ('[]','','null') OR visuals_json IN ('[]','','null'))"
    )
    if hint:
        # Stems that name the embedded asset in any common word; the full
        # sweep (hint=False) is the only one that can't miss any.
        sql += (" AND (stem LIKE '%graph%' OR stem LIKE '%figure%' OR stem LIKE '%diagram%'"
                " OR stem LIKE '%table%' OR stem LIKE '%chart%' OR stem LIKE '%scatterplot%'"
                " OR stem LIKE '%map%' OR stem LIKE '%illustration%' OR stem LIKE '%plot%')")
    sql += " ORDER BY id"
    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]
    stats = {"candidate": 0, "fetched": 0, "failed": 0, "now_visuals": 0,
             "now_images": 0, "now_tables": 0, "still_empty": 0,
             "unsupported_markup": 0, "missing_files": 0}
    for i, r in enumerate(rows, 1):
        stats["candidate"] += 1
        try:
            ext = json.loads(r["provenance_json"] or "{}").get("external_id") \
                or r["source_question_number"]
        except json.JSONDecodeError:
            ext = r["source_question_number"]
        if not ext:
            stats["failed"] += 1
            continue
        detail = fetch_question(str(ext))
        if detail is None:
            stats["failed"] += 1
            continue
        stats["fetched"] += 1
        # Reuse the full normalization so figure numbering/ordering matches
        # the normal ingest path (shared across stem and stimulus).
        row = _normalize(detail, {"external_id": str(ext)})
        visuals = row["visuals"] if row else []
        images = [v["file"] for v in visuals if v.get("kind") == "image"]
        # The audit reports visuals found in the live record but unsupported
        # by the extractor, plus any persisted image whose file is missing
        # from disk (acceptance criterion: no silently dropped visuals).
        if not visuals:
            stats["still_empty"] += 1
        for v in visuals:
            if v.get("kind") == "image" and not (config.IMAGES_DIR / v["file"]).exists():
                stats["missing_files"] += 1
        if not images:
            stats["unsupported_markup"] += 1
        if audit_only:
            if i % 50 == 0:
                print(f"  audit {i}/{len(rows)} … {stats}")
            time.sleep(sleep_s)
            continue
        if not visuals:
            # Checkpoint even when this candidate gained nothing, so an
            # interrupted run resumes where it left off (resumable sweep).
            if i % 50 == 0:
                conn.commit()
                print(f"  backfill {i}/{len(rows)} … {stats}")
            time.sleep(sleep_s)
            continue
        conn.execute("UPDATE questions SET images_json=?, visuals_json=? WHERE id=?",
                     (json.dumps(images), json.dumps(visuals), r["id"]))
        stats["now_visuals"] += 1
        stats["now_images"] += len(images)
        stats["now_tables"] += sum(1 for v in visuals if v.get("kind") == "table")
        if i % 50 == 0:
            conn.commit()
            print(f"  backfill {i}/{len(rows)} … {stats}")
        time.sleep(sleep_s)
    conn.commit()
    return stats


#: Backward-compatible alias: figure-only backfill is now the visual sweep.
def backfill_figures(conn, figure_hint: bool = True, limit: int = 0,
                     sleep_s: float = 0.25, **kwargs) -> dict:
    """Legacy name for `backfill_visuals` (issue #46 unified the sweep)."""
    return backfill_visuals(conn, hint=figure_hint, limit=limit,
                            sleep_s=sleep_s, **kwargs)


if __name__ == "__main__":
    from ..db import db_context

    with db_context() as _conn:
        print(fetch_qbank(_conn))
