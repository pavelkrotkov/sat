"""Tag ledger: the only module that knows what `question_tags.origin` means.

Tags reach a question from four directions (spec section 7):

    rule       deterministic tagging by tagger.py; recomputed on every run
    llm        cached out-of-band classification (llm_tag_cache)
    archive    restored from exports/corpus-v1.jsonl; behaves as `rule`
    manual     an admin correction; survives re-tagging
    suppressed an admin removal; the tag does NOT apply to this question

Readers want one thing - "which tags apply to this question?" - and must not
have to know that `suppressed` rows exist. Everything outside this module
reads through `effective_tags` / `tags_by_question`, or joins against the
EFFECTIVE_TAGS view; nobody else selects `origin`.

The single exception is `all_tags_with_origin`, for the two callers that
legitimately need the raw ledger: the admin screen (so a suppression can be
lifted) and the archive (so a restore is faithful).
"""

from ..clock import utc_now
from ..db import EFFECTIVE_TAGS as _EFFECTIVE_TAGS

ORIGIN_RULE = "rule"
ORIGIN_LLM = "llm"
ORIGIN_MANUAL = "manual"
ORIGIN_ARCHIVE = "archive"
ORIGIN_SUPPRESSED = "suppressed"

#: Origins that assert a tag applies. `suppressed` is deliberately absent.
APPLYING_ORIGINS = (ORIGIN_RULE, ORIGIN_LLM, ORIGIN_MANUAL, ORIGIN_ARCHIVE)

#: Which origin wins when two sources disagree about the same tag.
#: UNIQUE(question_id, tag) means only one row survives per pair, so this is
#: enforced at write time rather than read time.
ORIGIN_PRECEDENCE = {
    ORIGIN_RULE: 0,
    ORIGIN_ARCHIVE: 0,
    ORIGIN_LLM: 1,
    ORIGIN_MANUAL: 2,
    ORIGIN_SUPPRESSED: 3,
}

#: Join target for aggregate SQL that cannot reasonably move into Python.
#: Drop-in replacement for `question_tags`, minus suppressed rows. The view
#: itself is created with the rest of the schema - it is storage, and putting
#: its DDL here would make satprep.db import this package, which is the wrong
#: direction. What lives here is the meaning of `origin`; the view's exclusion
#: is pinned to ORIGIN_SUPPRESSED by a test.
EFFECTIVE_TAGS = _EFFECTIVE_TAGS


# ------------------------------------------------------------------ reads --


def effective_tags(conn, question_id: int) -> list[str]:
    """Tags that apply to one question, suppressions honoured."""
    return [
        r["tag"]
        for r in conn.execute(
            f"SELECT tag FROM {EFFECTIVE_TAGS} WHERE question_id=? ORDER BY tag",
            (question_id,),
        )
    ]


def tags_by_question(conn, question_ids=None) -> dict[int, list[str]]:
    """Bulk form of `effective_tags`.

    `question_ids=None` loads the whole corpus in one query - the sampler
    scores every candidate, so per-question round trips would dominate.
    Questions with no applying tags are absent from the result; callers use
    `.get(qid, [])`.
    """
    if question_ids is None:
        rows = conn.execute(f"SELECT question_id, tag FROM {EFFECTIVE_TAGS}")
    else:
        ids = list(question_ids)
        if not ids:
            return {}
        qmarks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT question_id, tag FROM {EFFECTIVE_TAGS} WHERE question_id IN ({qmarks})",
            ids,
        )
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["question_id"], []).append(r["tag"])
    return out


def all_tags_with_origin(conn, question_id: int) -> list[tuple[str, str]]:
    """Raw ledger rows including suppressions.

    Only two callers should need this: the admin screen (to offer un-suppress)
    and the archive (a restore must be faithful, suppressions included).
    """
    return [
        (r["tag"], r["origin"])
        for r in conn.execute(
            "SELECT tag, origin FROM question_tags WHERE question_id=? ORDER BY tag",
            (question_id,),
        )
    ]


# ----------------------------------------------------------------- writes --


def _upsert(conn, question_id: int, tag: str, origin: str) -> None:
    conn.execute(
        """INSERT INTO question_tags (question_id, tag, origin, created_at)
           VALUES (?,?,?,?)
           ON CONFLICT(question_id, tag) DO UPDATE SET origin=excluded.origin,
                                                       created_at=excluded.created_at""",
        (question_id, tag, origin, utc_now()),
    )


def set_manual(conn, question_id: int, tag: str) -> None:
    """Admin asserts a tag. Beats rule/llm and lifts an existing suppression."""
    _upsert(conn, question_id, tag, ORIGIN_MANUAL)


def suppress(conn, question_id: int, tag: str) -> None:
    """Admin removes a tag. The row is kept so re-tagging cannot resurrect it."""
    _upsert(conn, question_id, tag, ORIGIN_SUPPRESSED)


#: Origins that a re-tagging run may recompute. `archive` is derived rather
#: than decided - it is the fallback origin for legacy archive lines that
#: carry no origin of their own - so it is refreshed alongside `rule`.
#: Leaving it out would pin a restored tag in place forever, because
#: INSERT OR IGNORE cannot overwrite the row it left behind.
RECOMPUTABLE_ORIGINS = (ORIGIN_RULE, ORIGIN_ARCHIVE)


def set_rule_tags(conn, question_id: int, tags: list[str]) -> None:
    """Replace this question's rule-derived tags.

    Only derived rows are cleared: a manual correction or a suppression is a
    human decision about this question and outlives any number of re-tagging
    runs. INSERT OR IGNORE then leaves those rows alone.
    """
    qmarks = ",".join("?" for _ in RECOMPUTABLE_ORIGINS)
    conn.execute(
        f"DELETE FROM question_tags WHERE question_id=? AND origin IN ({qmarks})",
        (question_id, *RECOMPUTABLE_ORIGINS),
    )
    now = utc_now()
    for tag in tags:
        conn.execute(
            "INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,?,?)",
            (question_id, tag, ORIGIN_RULE, now),
        )


def restore_tag(conn, question_id: int, tag: str, origin: str) -> None:
    """Insert an archived tag verbatim, suppressions included."""
    conn.execute(
        "INSERT OR IGNORE INTO question_tags (question_id, tag, origin, created_at) VALUES (?,?,?,'')",
        (question_id, tag, origin if origin in ORIGIN_PRECEDENCE else ORIGIN_ARCHIVE),
    )
