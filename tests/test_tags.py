"""Tag ledger: precedence, suppression, and survival across re-tagging.

The guarantee under test is the one README advertises - "manual corrections
via the admin UI always win" - which means a correction has to reach the
sampler and the weakness model, not just the admin screen.
"""

import pytest

from satprep import tags as tagmod
from satprep.sampler import _load_candidates
from satprep.tagger import run_full_tagging
from satprep.weakness import compute_weakness
from conftest import add_question, add_attempt


@pytest.fixture()
def tagged(db):
    """One historical wrong answer on a question tagged qualifier_strength."""
    conn, path = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"],
                       source="bluebook_test", pool="historical",
                       tags=("qualifier_strength", "chronology"))
    add_attempt(conn, qid, correct=0)
    conn.commit()
    return conn, path, qid


# ------------------------------------------------------------------ reads --

def test_effective_tags_omits_suppressed(tagged):
    conn, _, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")

    assert tagmod.effective_tags(conn, qid) == ["chronology"]
    assert tagmod.tags_by_question(conn)[qid] == ["chronology"]


def test_tags_by_question_scoped_to_ids(tagged):
    conn, _, qid = tagged
    other = add_question(conn, passage="o", stem="o?", choices=["oa", "ob", "oc", "od"],
                         tags=("scope_shift",))

    assert set(tagmod.tags_by_question(conn, [qid])) == {qid}
    assert set(tagmod.tags_by_question(conn, [qid, other])) == {qid, other}
    assert tagmod.tags_by_question(conn, []) == {}


def test_question_with_only_suppressed_tags_is_absent(tagged):
    conn, _, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")
    tagmod.suppress(conn, qid, "chronology")

    assert tagmod.effective_tags(conn, qid) == []
    assert qid not in tagmod.tags_by_question(conn)


def test_all_tags_with_origin_still_shows_suppressed(tagged):
    """The admin screen needs the raw row to offer un-suppress."""
    conn, _, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")

    assert tagmod.all_tags_with_origin(conn, qid) == [
        ("chronology", "rule"),
        ("qualifier_strength", "suppressed"),
    ]


# ----------------------------------------------------- reaches the readers --

def test_suppressed_tag_leaves_the_weakness_model(tagged):
    """Regression: compute_weakness joined question_tags directly and scored
    suppressed tags as if the admin had never touched them."""
    conn, _, qid = tagged
    assert "qualifier_strength" in compute_weakness(conn)["tag"]

    tagmod.suppress(conn, qid, "qualifier_strength")
    assert "qualifier_strength" not in compute_weakness(conn)["tag"]


def test_suppressed_tag_leaves_the_sampler(tagged):
    """Regression: _load_candidates built its tag map from the raw table."""
    conn, _, qid = tagged
    assert "qualifier_strength" in _load_candidates(conn, ("historical",))[0].tags

    tagmod.suppress(conn, qid, "qualifier_strength")
    assert _load_candidates(conn, ("historical",))[0].tags == ["chronology"]


def test_manual_tag_reaches_the_sampler(tagged):
    conn, _, qid = tagged
    tagmod.set_manual(conn, qid, "cause_vs_correlation")

    assert "cause_vs_correlation" in _load_candidates(conn, ("historical",))[0].tags


# ------------------------------------------------- survival across retagging --

def test_suppression_survives_full_tagging(tagged):
    """A re-tagging run must not resurrect a tag the admin removed."""
    conn, path, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")
    conn.commit()

    run_full_tagging(conn)

    assert "qualifier_strength" not in tagmod.effective_tags(conn, qid)


def test_manual_tag_survives_full_tagging(tagged):
    conn, path, qid = tagged
    tagmod.set_manual(conn, qid, "cause_vs_correlation")
    conn.commit()

    run_full_tagging(conn)

    assert "cause_vs_correlation" in tagmod.effective_tags(conn, qid)


def test_set_manual_lifts_a_suppression(tagged):
    """Un-suppress is 'assert it manually', not 'delete the row'."""
    conn, _, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")
    tagmod.set_manual(conn, qid, "qualifier_strength")

    assert "qualifier_strength" in tagmod.effective_tags(conn, qid)
    assert dict(tagmod.all_tags_with_origin(conn, qid))["qualifier_strength"] == "manual"


def test_set_rule_tags_replaces_only_rule_rows(tagged):
    conn, _, qid = tagged
    tagmod.set_manual(conn, qid, "scope_shift")
    tagmod.suppress(conn, qid, "chronology")

    tagmod.set_rule_tags(conn, qid, ["tone_or_stance"])

    assert dict(tagmod.all_tags_with_origin(conn, qid)) == {
        "tone_or_stance": "rule",     # new rule tag
        "scope_shift": "manual",      # human assertion kept
        "chronology": "suppressed",   # human removal kept
    }
    assert "qualifier_strength" not in tagmod.effective_tags(conn, qid)


# ---------------------------------------------------------------- archive --

def test_archive_round_trip_preserves_suppression(tagged, tmp_path):
    """A restore must be faithful: a suppressed tag stays suppressed, or the
    correction is silently lost on rebuild."""
    from satprep.archive import export_corpus, restore_corpus
    from satprep.db import connect

    conn, path, qid = tagged
    tagmod.suppress(conn, qid, "qualifier_strength")
    conn.commit()

    archive = export_corpus(conn, out_path=tmp_path / "corpus.jsonl")
    restored_path = tmp_path / "restored.db"
    fresh = connect(restored_path)
    restore_corpus(fresh, archive_path=archive)
    fresh.commit()
    new_qid = fresh.execute("SELECT id FROM questions").fetchone()["id"]
    assert dict(tagmod.all_tags_with_origin(fresh, new_qid))["qualifier_strength"] == "suppressed"
    assert tagmod.effective_tags(fresh, new_qid) == ["chronology"]
    fresh.close()


def test_set_rule_tags_refreshes_archive_origin_rows(tagged):
    """A legacy archive line carries no origin of its own, so restore_tag
    falls back to `archive`. That is a derived tag, not a decision - if
    re-tagging skipped it, INSERT OR IGNORE could never replace the row and
    an obsolete restored tag would stay effective forever."""
    conn, _, qid = tagged
    tagmod.restore_tag(conn, qid, "stale_restored_tag", "archive")
    assert "stale_restored_tag" in tagmod.effective_tags(conn, qid)

    tagmod.set_rule_tags(conn, qid, ["tone_or_stance"])

    assert "stale_restored_tag" not in tagmod.effective_tags(conn, qid)
    assert tagmod.effective_tags(conn, qid) == ["tone_or_stance"]


def test_admin_tag_correction_refreshes_weakness_cache(tagged):
    """select_drill and /weaknesses both prefer weakness_cache over
    recomputing, so a correction that leaves it stale is half applied."""
    import satprep.server as server_mod

    from satprep.db import connect

    conn, path, qid = tagged
    compute_weakness(conn)
    conn.commit()
    conn.close()

    def cached(tag):
        # read on a fresh connection so we see committed state
        c = connect(path)
        n = c.execute(
            "SELECT COUNT(*) FROM weakness_cache WHERE entity_type='tag' AND entity=?",
            (tag,),
        ).fetchone()[0]
        c.close()
        return n

    assert cached("qualifier_strength") == 1

    handler_conn = connect(path)
    server_mod.admin_tags_save(None, qid, tag="qualifier_strength",
                               action="remove", conn=handler_conn)
    handler_conn.commit()
    handler_conn.close()

    assert cached("qualifier_strength") == 0
    assert cached("chronology") == 1  # untouched associations survive
