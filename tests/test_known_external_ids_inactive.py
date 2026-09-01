"""Regression test for known_external_ids and the live fetch path.

Codex P2 finding on PR #41: the new active=1 filter on the external-identity
pass inside insert_qbank_row() cannot produce the intended fresh active row
via the normal `satprep fetch-qbank` path, because known_external_ids() selects
from ALL rows (no active filter) and so a deactivated row's external_id still
blocks the re-fetch.

This test pins the contract: known_external_ids() must exclude inactive rows
so that a deactivated item can be re-fetched as fresh.
"""

from satprep.corpus.qbank_fetch import _normalize, insert_qbank_row, known_external_ids

DETAIL = {
    "type": "mcq",
    "stem": "<p>Which choice most logically completes the text?</p>",
    "stimulus": "<p>A stimulus &mdash; with entities.</p>",
    "answerOptions": [
        {"id": "k1", "content": "<p>opt A</p>"},
        {"id": "k2", "content": "<p>opt B</p>"},
        {"id": "k3", "content": "<p>opt C</p>"},
        {"id": "k4", "content": "<p>opt D</p>"},
    ],
    "keys": ["k2"],
    "rationale": "<p>Choice B is the best answer.</p>",
    "externalid": "ext-1",
}

META = {
    "external_id": "ext-1",
    "primary_class_cd_desc": "Information and Ideas",
    "skill_desc": "Inferences",
    "difficulty": "H",
}


def test_known_external_ids_excludes_inactive(db):
    """A deactivated row's external_id must NOT block re-fetch."""
    conn, _ = db
    # insert an active row, then deactivate it
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    conn.execute("UPDATE questions SET active=0")
    # the deactivated external_id must NOT show up in known_external_ids,
    # otherwise the live fetch path skips this item forever
    assert "ext-1" not in known_external_ids(conn)


def test_known_external_ids_includes_active(db):
    """Sanity: an active row's external_id IS in the known set."""
    conn, _ = db
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    assert "ext-1" in known_external_ids(conn)
