from satprep.corpus.qbank_fetch import _normalize, insert_qbank_row
from conftest import add_question

import json
import sqlite3

import pytest


META = {"external_id": "ext-1", "primary_class_cd_desc": "Information and Ideas",
        "skill_desc": "Inferences", "difficulty": "H"}
DETAIL = {
    "type": "mcq",
    "stem": "<p>Which choice most logically completes the text?</p>",
    "stimulus": "<p>A stimulus &mdash; with entities.</p>",
    "answerOptions": [{"id": "k1", "content": "<p>opt A</p>"},
                      {"id": "k2", "content": "<p>opt B</p>"},
                      {"id": "k3", "content": "<p>opt C</p>"},
                      {"id": "k4", "content": "<p>opt D</p>"}],
    "keys": ["k2"],
    "rationale": "<p>Choice B is the best answer.</p>",
    "externalid": "ext-1",
}


def test_normalize_extracts_everything():
    row = _normalize(DETAIL, META)
    assert row["correct"] == "B"
    assert [c["letter"] for c in row["choices"]] == list("ABCD")
    assert row["choices"][1]["is_correct"]
    assert row["difficulty"] == "hard"
    assert row["skill"] == "Inferences"
    assert "&mdash;" not in row["passage"] and "—" in row["passage"]


def test_normalize_rejects_missing_key():
    bad = {k: v for k, v in DETAIL.items() if k != "keys"}
    assert _normalize(bad, META) is None


def test_insert_dedupes_and_pools(db):
    conn, path = db
    row = _normalize(DETAIL, META)
    first = insert_qbank_row(conn, row, batch="b1")
    second = insert_qbank_row(conn, row, batch="b2")
    assert first == "added" and second == "duplicate"
    r = conn.execute("SELECT pool, is_new_bank, import_batch FROM questions").fetchone()
    assert r["pool"] in ("fresh_training", "protected_benchmark")
    assert r["is_new_bank"] == 1 and r["import_batch"] == "b1"


def test_insert_rejects_incomplete(db):
    conn, path = db
    assert insert_qbank_row(conn, {"choices": [], "correct": ""}, batch="b") == "invalid"


def test_known_external_ids_roundtrip(db):
    from satprep.corpus.qbank_fetch import known_external_ids

    conn, path = db
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    assert "ext-1" in known_external_ids(conn)


def test_cross_source_duplicate_enriches_choiceless_row(db):
    """Greptile P1: bank item matching a choice-less Bluebook row backfills choices."""
    from satprep.corpus.fingerprint import fingerprint
    from satprep.corpus.qbank_fetch import insert_qbank_row

    # historical stats-only row: no choices, unknown difficulty
    add_question(db[0], passage="shared passage", stem="shared stem?",
                 choices=[], correct="A", source="bluebook_test", pool="historical")
    # force the bank row to collide: compute fingerprint of its content and pre-insert
    from satprep.db import connect as c2
    conn = db[0]
    conn.execute("DELETE FROM questions")
    conn.execute("""INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                     module, passage, stem, choices_json, correct_letter, rationale, images_json,
                     official_domain, official_skill, skill_source, difficulty, pool,
                     seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
                   VALUES ('preseed', 'bluebook_test', 'SAT Practice Test 4', '7', '',
                     'shared passage', 'shared stem?', '[]', 'A', '', '[]',
                     '', '', 'unknown', '', 'historical', 0, 0, '', '2026-01-01', '{}')""")
    fp = fingerprint("shared passage", "shared stem?", ["opt a", "opt b", "opt c", "opt d"])
    # the incoming bank row must hash identically: same passage/stem/choice texts
    row = {"passage": "shared passage", "stem": "shared stem?",
           "choices": [{"letter": l, "text": t, "is_correct": l == "B"}
                       for l, t in zip("ABCD", ["opt a", "opt b", "opt c", "opt d"])],
           "correct": "B", "difficulty": "hard", "skill": "Inferences",
           "domain": "Information and Ideas", "ext_id": "ext-9"}
    outcome = insert_qbank_row(conn, row, batch="b9")
    assert outcome == "duplicate"
    r = conn.execute("SELECT choices_json, difficulty, official_skill, pool, is_new_bank FROM questions WHERE fingerprint='preseed'").fetchone()
    import json as _json
    assert len(_json.loads(r["choices_json"])) == 4   # enriched, now displayable


# ------------------------------------------------- figures from EQB (issue: imageless graph stems) --

SVG = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
       "<rect width='100' height='100'/>"
       "<text x='5' y='10'>yield (tons)</text></svg>")

DETAIL_FIG = {
    "type": "mcq",
    "stem": "<p>Which choice most effectively uses data from the graph to complete the text?</p>",
    "stimulus": f"<p>The scatterplot shows yield versus rainfall.</p><figure>{SVG}</figure>",
    "answerOptions": [{"id": "k1", "content": "<p>opt A</p>"},
                      {"id": "k2", "content": "<p>opt B</p>"},
                      {"id": "k3", "content": "<p>opt C</p>"},
                      {"id": "k4", "content": "<p>opt D</p>"}],
    "keys": ["k3"],
    "rationale": "<p>Choice C is the best answer.</p>",
    "externalid": "ext-fig",
}


@pytest.fixture()
def fig_dirs(tmp_path, monkeypatch):
    """Route figure files and the served images dir into tmp_path."""
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr("satprep.corpus.qbank_fetch.FIGURE_DIR", images)
    return images


FIG_META = dict(META, external_id="ext-fig")


def test_normalize_extracts_svg_figure_and_keeps_visible_text(fig_dirs):
    """The EQB embeds figures in the stimulus HTML. _clean_html strips every
    tag, which deleted the graph while its 'uses data from the graph' stem
    survived - rendering an unanswerable question. Figures must be saved and
    the markup must not leak into the cleaned text. Visible text inside the
    figure (axis labels) is KEPT so the passage matches what the old ingest
    stored (fingerprint stability) and what the student sees."""
    row = _normalize(DETAIL_FIG, FIG_META)

    assert len(row["images"]) == 1
    name = row["images"][0]
    saved = fig_dirs / name
    assert saved.exists() and saved.stat().st_size > 0
    assert b"<svg" in saved.read_bytes()
    # no markup leaked into the cleaned text
    assert "<svg" not in row["passage"] and "rect" not in row["passage"]
    assert "<svg" not in row["stem"]
    # the visible text around the figure is intact
    assert "scatterplot shows yield" in row["passage"]
    # text inside the svg (axis label) is preserved
    assert "yield (tons)" in row["passage"]


def test_normalize_without_figures_leaves_images_empty(fig_dirs):
    row = _normalize(DETAIL, META)
    assert row["images"] == []
    assert not any(fig_dirs.iterdir())


def test_normalize_extracts_data_uri_image_and_skips_malformed(fig_dirs):
    """<img src=\"data:image/png;base64,...\"> figures are saved; malformed
    data URIs (no base64 payload / bad base64) are skipped without crashing,
    and no unknown file suffix can reach disk."""
    import base64 as b64

    png = b64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    detail = {
        **DETAIL,
        "stimulus": (
            f'<img src="data:image/png;base64,{png}">'
            '<img src="data:image/png;base64,not-valid-base64!!!">'
            '<img src="data:image/svg%0a%0aevil">'  # no payload at all
        ),
        "externalid": "ext-img",
    }
    meta = dict(META, external_id="ext-img")
    row = _normalize(detail, meta)

    assert len(row["images"]) == 1
    assert row["images"][0].endswith(".png")
    saved = fig_dirs / row["images"][0]
    assert saved.read_bytes() == b"\x89PNG\r\n\x1a\n"
    # malformed img tags were left as text, not written anywhere
    assert [p.suffix for p in fig_dirs.iterdir()] == [".png"]


def test_normalize_saves_multiline_base64_data_uri(fig_dirs):
    """Issue #31: long data-URI payloads commonly serialize with newlines
    every ~76 chars. The capture regex spans them, but
    b64decode(validate=True) raises on any whitespace, so the figure was
    silently dropped and the student got a text-only question. Whitespace
    inside the payload must be stripped before decoding."""
    import base64 as b64

    payload = b64.b64encode(b"\x89PNG\r\n\x1a\n" + bytes(range(200))).decode()
    wrapped = "\n".join(payload[i:i + 40] for i in range(0, len(payload), 40))
    assert "\n" in wrapped  # the shape that used to be dropped
    detail = {
        **DETAIL,
        "stimulus": f'<img src="data:image/png;base64,{wrapped}">',
        "externalid": "ext-wrapped",
    }
    row = _normalize(detail, dict(META, external_id="ext-wrapped"))

    assert len(row["images"]) == 1, "wrapped base64 payload was silently dropped"
    saved = fig_dirs / row["images"][0]
    assert saved.read_bytes() == b"\x89PNG\r\n\x1a\n" + bytes(range(200))


def test_normalize_skips_whitespace_only_payload(fig_dirs):
    """A data URI whose base64 payload is only whitespace strips to an empty
    string. b64decode('', validate=True) returns b'' (no error), so without
    an explicit guard the extraction path would save a zero-byte image and
    replace the markup. It must skip like any other malformed payload."""
    detail = {
        **DETAIL,
        "stimulus": (
            '<img src="data:image/png;base64,   \n  ">'
            '<img src="data:image/png;base64,">'
        ),
        "externalid": "ext-blank",
    }
    row = _normalize(detail, dict(META, external_id="ext-blank"))

    assert row["images"] == []
    assert not any(fig_dirs.iterdir()), "whitespace-only payload must not be saved"


def test_figures_in_both_fields_get_unique_numbers(fig_dirs):
    """A question with a figure in BOTH stem and stimulus must produce two
    distinct files, not two names colliding on -figure-1 (which would
    overwrite the first figure with the second)."""
    detail = {
        **DETAIL,
        "stem": f"See the graph. <figure>{SVG}</figure>",
        "stimulus": f"<p>Second panel.</p><figure>{SVG}</figure>",
        "externalid": "ext-two",
    }
    row = _normalize(detail, dict(META, external_id="ext-two"))
    assert len(row["images"]) == 2
    assert len(set(row["images"])) == 2, row["images"]
    for n in row["images"]:
        assert (fig_dirs / n).exists()
    names = [n.rsplit("-", 1)[1] for n in row["images"]]
    assert names == ["1.svg", "2.svg"]


def test_mixed_formats_extracted_in_document_order(fig_dirs):
    """A data-URI <img> followed by an inline <svg> in the same field must
    be saved in source order — the template renders images in list order,
    so 'the first graph' references must match the display."""
    import base64 as b64
    png = b64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    detail = {
        **DETAIL,
        "stimulus": (
            f'<figure><img src="data:image/png;base64,{png}"></figure>'
            f"<figure>{SVG}</figure>"
        ),
        "externalid": "ext-mix",
    }
    row = _normalize(detail, dict(META, external_id="ext-mix"))
    assert [n.rsplit(".", 1)[1] for n in row["images"]] == ["png", "svg"]
    assert [n.rsplit("-", 1)[1].split(".")[0] for n in row["images"]] == ["1", "2"]


def test_figure_with_caption_keeps_caption_text(fig_dirs):
    """A figure whose content is an extracted image plus a <figcaption> must
    not lose the caption: the old _clean_html path kept it, and dropping it
    would make some questions incomplete."""
    detail = {
        **DETAIL,
        "stimulus": (f"<p>Preamble.</p>"
                     f"<figure>{SVG}<figcaption>Figure 1. Yield by season.</figcaption></figure>"
                     "<p>Postamble.</p>"),
        "externalid": "ext-cap",
    }
    row = _normalize(detail, dict(META, external_id="ext-cap"))
    assert len(row["images"]) == 1
    assert "Figure 1. Yield by season." in row["passage"]
    assert "Preamble." in row["passage"] and "Postamble." in row["passage"]


def test_external_identity_reconciles_on_reimport(db, fig_dirs):
    """Figure extraction changes the stored passage/stem text, so a re-import
    of a known bank item can miss the content fingerprint. The external_id
    is canonical: matching on it must reconcile (duplicate) instead of
    inserting a second, fresh-pool copy of the same question."""
    from satprep.corpus.fingerprint import fingerprint

    conn, path = db
    # legacy row: text as the OLD ingest stored it (no figure markup), no images
    legacy_passage = "The scatterplot shows yield versus rainfall."
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES ('legacy', 'college_board_question_bank', 'ext-fig', '', '',
             ?, 'Which choice most effectively uses data from the graph to complete the text?',
             '["opt A","opt B","opt C","opt D"]', 'C', '', '[]',
             '', 'Inferences', 'metadata', 'hard', 'protected_benchmark',
             0, 1, 'old', '2026-01-01', ?)""",
        (legacy_passage, json.dumps({"external_id": "ext-fig"})),
    )
    # new ingest: passage now contains the preserved figure text, images attached
    row = _normalize(DETAIL_FIG, FIG_META)
    outcome = insert_qbank_row(conn, row, batch="reimport")

    assert outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    r = conn.execute("SELECT images_json FROM questions WHERE fingerprint='legacy'").fetchone()
    assert json.loads(r["images_json"]) == row["images"], \
        "re-import by external identity did not backfill the figures"


def test_insert_persists_figures_into_images_json(db, fig_dirs):
    conn, path = db
    row = _normalize(DETAIL_FIG, FIG_META)
    insert_qbank_row(conn, row, batch="bfig")

    r = conn.execute("SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'").fetchone()
    assert json.loads(r["images_json"]) == row["images"]


def test_duplicate_reconcile_backfills_missing_figures(db, fig_dirs):
    """A question ingested before figure extraction has images_json='[]'; when
    the same bank item is re-imported (with figures now extracted), the
    existing row must pick them up."""
    conn, path = db
    # pre-seed the same question as an imageless row (as the old ingest left it)
    from satprep.corpus.fingerprint import fingerprint

    bare = {"passage": "The scatterplot shows yield versus rainfall.",
            "stem": "Which choice most effectively uses data from the graph to complete the text?",
            "choices": [{"letter": l, "text": t, "is_correct": l == "C"}
                        for l, t in zip("ABCD", ["opt A", "opt B", "opt C", "opt D"])],
            "correct": "C", "difficulty": "hard", "skill": "Inferences",
            "domain": "Information and Ideas", "ext_id": "ext-fig"}
    fp = fingerprint(bare["passage"], bare["stem"], ["opt A", "opt B", "opt C", "opt D"])
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?, 'college_board_question_bank', 'ext-fig', '', '',
             ?, ?, ?, 'C', '', '[]',
             '', 'Inferences', 'metadata', 'hard', 'fresh_training',
             0, 1, 'old', '2026-01-01', '{"external_id":"ext-fig"}')""",
        (fp, bare["passage"], bare["stem"], json.dumps(bare["choices"])),
    )
    conn.commit()

    # re-import the same item, now with a figure
    row = _normalize(DETAIL_FIG, FIG_META)
    outcome = insert_qbank_row(conn, row, batch="bfig2")
    assert outcome == "duplicate"

    r = conn.execute("SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'").fetchone()
    assert json.loads(r["images_json"]) == row["images"], \
        "re-import did not backfill figures onto the imageless row"


def test_backfill_figures_attaches_figures_to_imageless_rows(db, fig_dirs, monkeypatch):
    """backfill_figures re-fetches stored bank questions that lost their
    figures and attaches them, without touching rows that have none."""
    from satprep.corpus import qbank_fetch

    conn, path = db
    # one figure-bearing bank row stored imageless (the pre-fix backlog),
    # and one genuinely figure-less row that must stay empty.
    figrow = _normalize(DETAIL_FIG, FIG_META)
    insert_qbank_row(conn, figrow, batch="bfill")
    # simulate the pre-fix ingest that dropped the figure
    conn.execute("UPDATE questions SET images_json='[]' WHERE provenance_json LIKE '%ext-fig%'")
    conn.execute("""INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                     module, passage, stem, choices_json, correct_letter, rationale, images_json,
                     official_domain, official_skill, skill_source, difficulty, pool,
                     seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
                   VALUES ('plain', 'college_board_question_bank', 'ext-plain', '', '',
                     'A plain prompt.', 'Which completes the text?', '["one","two","three","four"]',
                     'A', '', '[]', '', 'Inferences', 'metadata', 'medium', 'fresh_training',
                     0, 1, 'old', '2026-01-01', '{"external_id":"ext-plain"}')""")
    conn.commit()

    def fake_fetch(ext):
        if str(ext) == "ext-fig":
            return DETAIL_FIG
        return DETAIL   # ext-plain: no figure

    monkeypatch.setattr(qbank_fetch, "fetch_question", fake_fetch)

    stats = qbank_fetch.backfill_figures(conn, figure_hint=False, sleep_s=0)

    assert stats["now_images"] == 1
    assert stats["still_empty"] >= 1
    # the figure-bearing row gained its figure
    figur = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'").fetchone()
    assert json.loads(figur["images_json"]) == figrow["images"]
    # the figure-less row stayed empty
    plain = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-plain%'").fetchone()
    assert plain["images_json"] == "[]"
