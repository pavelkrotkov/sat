import json

import pytest
from conftest import add_question

from satprep.corpus.qbank_fetch import _normalize, insert_qbank_row

META = {
    "external_id": "ext-1",
    "primary_class_cd_desc": "Information and Ideas",
    "skill_desc": "Inferences",
    "difficulty": "H",
}
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


def test_normalize_extracts_everything():
    row = _normalize(DETAIL, META)
    assert row is not None
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
    conn, _path = db
    row = _normalize(DETAIL, META)
    first = insert_qbank_row(conn, row, batch="b1")
    second = insert_qbank_row(conn, row, batch="b2")
    assert first == "added" and second == "duplicate"
    r = conn.execute("SELECT pool, is_new_bank, import_batch FROM questions").fetchone()
    assert r["pool"] in ("fresh_training", "protected_benchmark")
    assert r["is_new_bank"] == 1 and r["import_batch"] == "b1"


def test_insert_rejects_incomplete(db):
    conn, _path = db
    assert insert_qbank_row(conn, {"choices": [], "correct": ""}, batch="b") == "invalid"


def test_known_external_ids_roundtrip(db):
    from satprep.corpus.qbank_fetch import known_external_ids

    conn, _path = db
    insert_qbank_row(conn, _normalize(DETAIL, META), batch="b1")
    assert "ext-1" in known_external_ids(conn)


def test_cross_source_duplicate_enriches_choiceless_row(db):
    """Greptile P1: bank item matching a choice-less Bluebook row backfills choices."""
    from satprep.corpus.fingerprint import fingerprint
    from satprep.corpus.qbank_fetch import insert_qbank_row

    # historical stats-only row: no choices, unknown difficulty
    add_question(
        db[0],
        passage="shared passage",
        stem="shared stem?",
        choices=[],
        correct="A",
        source="bluebook_test",
        pool="historical",
    )
    # force the bank row to collide: compute fingerprint of its content and pre-insert
    conn = db[0]
    conn.execute("DELETE FROM questions")
    conn.execute("""INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                     module, passage, stem, choices_json, correct_letter, rationale, images_json,
                     official_domain, official_skill, skill_source, difficulty, pool,
                     seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
                   VALUES ('preseed', 'bluebook_test', 'SAT Practice Test 4', '7', '',
                     'shared passage', 'shared stem?', '[]', 'A', '', '[]',
                     '', '', 'unknown', '', 'historical', 0, 0, '', '2026-01-01', '{}')""")
    fingerprint("shared passage", "shared stem?", ["opt a", "opt b", "opt c", "opt d"])
    # the incoming bank row must hash identically: same passage/stem/choice texts
    row = {
        "passage": "shared passage",
        "stem": "shared stem?",
        "choices": [
            {"letter": ch, "text": t, "is_correct": ch == "B"}
            for ch, t in zip("ABCD", ["opt a", "opt b", "opt c", "opt d"], strict=False)
        ],
        "correct": "B",
        "difficulty": "hard",
        "skill": "Inferences",
        "domain": "Information and Ideas",
        "ext_id": "ext-9",
    }
    outcome = insert_qbank_row(conn, row, batch="b9")
    assert outcome == "duplicate"
    r = conn.execute(
        "SELECT choices_json, difficulty, official_skill, pool, is_new_bank FROM questions WHERE fingerprint='preseed'"
    ).fetchone()
    import json as _json

    assert len(_json.loads(r["choices_json"])) == 4  # enriched, now displayable


# ------------------------------------------------- figures from EQB (issue: imageless graph stems) --

SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<rect width='100' height='100'/>"
    "<text x='5' y='10'>yield (tons)</text></svg>"
)

DETAIL_FIG = {
    "type": "mcq",
    "stem": "<p>Which choice most effectively uses data from the graph to complete the text?</p>",
    "stimulus": f"<p>The scatterplot shows yield versus rainfall.</p><figure>{SVG}</figure>",
    "answerOptions": [
        {"id": "k1", "content": "<p>opt A</p>"},
        {"id": "k2", "content": "<p>opt B</p>"},
        {"id": "k3", "content": "<p>opt C</p>"},
        {"id": "k4", "content": "<p>opt D</p>"},
    ],
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
    wrapped = "\n".join(payload[i : i + 40] for i in range(0, len(payload), 40))
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
        "stimulus": ('<img src="data:image/png;base64,   \n  "><img src="data:image/png;base64,">'),
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
            f'<figure><img src="data:image/png;base64,{png}"></figure><figure>{SVG}</figure>'
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
        "stimulus": (
            f"<p>Preamble.</p>"
            f"<figure>{SVG}<figcaption>Figure 1. Yield by season.</figcaption></figure>"
            "<p>Postamble.</p>"
        ),
        "externalid": "ext-cap",
    }
    row = _normalize(detail, dict(META, external_id="ext-cap"))
    assert len(row["images"]) == 1
    assert "Figure 1. Yield by season." in row["passage"]
    assert "Preamble." in row["passage"] and "Postamble." in row["passage"]


def test_external_identity_reconciles_on_reimport(db, fig_dirs):
    """The content-fingerprint path handles byte-identical re-imports. This test
    exercises the external_identity path for its real purpose: server-side
    content drift (College Board rephrasing a question between fetches) keyed
    on the canonical external_id — the stored row has a passage that does NOT
    fingerprint-match the new ingest, but the external_id still reconciles it
    instead of inserting a fresh duplicate (which could land in protected
    benchmark)."""

    conn, _path = db
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
    assert json.loads(r["images_json"]) == row["images"], (
        "re-import by external identity did not backfill the figures"
    )


def test_external_identity_skips_inactive_row(db, fig_dirs):
    """A deactivated (active=0) row sharing the same external_id must NOT be
    picked as the reconciliation target; if the only matching row is inactive,
    the function must fall through to insert a new question. The new row then
    stays eligible for the normal fingerprint/loose-reconcile paths on a later
    re-import, while the deactivated row stays untouched (audit history)."""

    conn, _path = db
    # deactivated row with the same external_id we will re-import
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json, active)
           VALUES ('legacy', 'college_board_question_bank', 'ext-fig', '', '',
             'old passage', 'old stem', '["a","b","c","d"]', 'A', '', '[]',
             '', 'Inferences', 'metadata', 'hard', 'fresh_training',
             0, 1, 'old', '2026-01-01', '{"external_id":"ext-fig"}', 0)"""
    )
    # new ingest with the same external_id but different content
    row = _normalize(DETAIL_FIG, FIG_META)
    outcome = insert_qbank_row(conn, row, batch="reimport")

    # The deactivated row is not a valid reconciliation target. The new ingest
    # does not fingerprint-match the deactivated row's passage/stem, so the
    # external-identity pass must skip it and a fresh row is inserted.
    assert outcome == "added"
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
    # the deactivated row is left untouched (still active=0, still old passage)
    dead = conn.execute(
        "SELECT active, passage FROM questions WHERE fingerprint='legacy'"
    ).fetchone()
    assert dead["active"] == 0
    assert dead["passage"] == "old passage"
    # the new row is active=1 and carries the new content
    live = conn.execute("SELECT active, images_json FROM questions WHERE active=1").fetchone()
    assert live["active"] == 1
    assert json.loads(live["images_json"]) == row["images"]


def test_external_identity_picks_most_recently_ingested_on_duplicate(db, fig_dirs):
    """When two active rows somehow share an external_id (data anomaly; the
    loose-reconcile and content-fingerprint paths should usually prevent this),
    the external-identity pass must deterministically pick the most recently
    ingested row (ORDER BY id DESC) — the older row stays untouched."""

    conn, _path = db
    # Two legacy active rows for the same external_id: older (id=1) and newer (id=2).
    for n, passage in [(1, "older passage"), (2, "newer passage")]:
        conn.execute(
            """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                 module, passage, stem, choices_json, correct_letter, rationale, images_json,
                 official_domain, official_skill, skill_source, difficulty, pool,
                 seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
               VALUES (?, 'college_board_question_bank', 'ext-fig', '', '',
                 ?, 'stem text here', '["a","b","c","d"]', 'A', '', '[]',
                 '', 'Inferences', 'metadata', 'hard', 'fresh_training',
                 0, 1, 'old', '2026-01-01', '{"external_id":"ext-fig"}')""",
            (f"legacy-{n}", passage),
        )
    # new ingest: external_id matches both legacy rows; most recent (id=2) wins.
    row = _normalize(DETAIL_FIG, FIG_META)
    outcome = insert_qbank_row(conn, row, batch="reimport")

    assert outcome == "duplicate"
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2
    # The newer legacy row gained the figure; the older one is untouched.
    older = conn.execute("SELECT images_json FROM questions WHERE id=1").fetchone()
    newer = conn.execute("SELECT images_json FROM questions WHERE id=2").fetchone()
    assert older["images_json"] == "[]"
    assert json.loads(newer["images_json"]) == row["images"]


def test_duplicate_reconcile_preserves_existing_figures(db, fig_dirs):
    """Re-import must not overwrite an already-figure-bearing row. _backfill_images
    short-circuits when the stored images_json is non-empty; a regression that
    overwrites good figures with a different list from a later fetch must fail
    this test."""
    from satprep.corpus.fingerprint import fingerprint

    conn, _path = db
    # pre-seed a row that already has a figure list (different from what
    # _normalize would extract from DETAIL_FIG), so a regression that overwrites
    # would surface as a list change.
    existing_figs = ["original-a.svg", "original-b.svg"]
    fp = fingerprint(
        "The scatterplot shows yield versus rainfall.",
        "Which choice most effectively uses data from the graph to complete the text?",
        ["opt A", "opt B", "opt C", "opt D"],
    )
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?, 'college_board_question_bank', 'ext-fig', '', '',
             'The scatterplot shows yield versus rainfall.',
             'Which choice most effectively uses data from the graph to complete the text?',
             '["opt A","opt B","opt C","opt D"]', 'C', '', ?,
             '', 'Inferences', 'metadata', 'hard', 'fresh_training',
             0, 1, 'old', '2026-01-01', '{"external_id":"ext-fig"}')""",
        (fp, json.dumps(existing_figs)),
    )
    conn.commit()

    # Re-import the same external_id; the freshly extracted figure list is
    # different from the original. The dedup path must short-circuit and
    # leave the existing list intact.
    row = _normalize(DETAIL_FIG, FIG_META)
    assert row["images"] != existing_figs, "test fixture must produce a different figure list"
    outcome = insert_qbank_row(conn, row, batch="reimport")
    assert outcome == "duplicate"

    r = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'"
    ).fetchone()
    assert json.loads(r["images_json"]) == existing_figs, (
        "re-import overwrote an already-figure-bearing row's images_json"
    )


def test_insert_persists_figures_into_images_json(db, fig_dirs):
    conn, _path = db
    row = _normalize(DETAIL_FIG, FIG_META)
    insert_qbank_row(conn, row, batch="bfig")

    r = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'"
    ).fetchone()
    assert json.loads(r["images_json"]) == row["images"]


def test_duplicate_reconcile_backfills_missing_figures(db, fig_dirs):
    """A question ingested before figure extraction has images_json='[]'; when
    the same bank item is re-imported (with figures now extracted), the
    existing row must pick them up."""
    conn, _path = db
    # pre-seed the same question as an imageless row (as the old ingest left it)
    from satprep.corpus.fingerprint import fingerprint

    bare = {
        "passage": "The scatterplot shows yield versus rainfall.",
        "stem": "Which choice most effectively uses data from the graph to complete the text?",
        "choices": [
            {"letter": ch, "text": t, "is_correct": ch == "C"}
            for ch, t in zip("ABCD", ["opt A", "opt B", "opt C", "opt D"], strict=False)
        ],
        "correct": "C",
        "difficulty": "hard",
        "skill": "Inferences",
        "domain": "Information and Ideas",
        "ext_id": "ext-fig",
    }
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

    r = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'"
    ).fetchone()
    assert json.loads(r["images_json"]) == row["images"], (
        "re-import did not backfill figures onto the imageless row"
    )


def test_backfill_figures_attaches_figures_to_imageless_rows(db, fig_dirs, monkeypatch):
    """backfill_figures re-fetches stored bank questions that lost their
    figures and attaches them, without touching rows that have none."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
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
        return DETAIL  # ext-plain: no figure

    monkeypatch.setattr(qbank_fetch, "fetch_question", fake_fetch)

    stats = qbank_fetch.backfill_figures(conn, figure_hint=False, sleep_s=0)

    assert stats["now_images"] == 1
    assert stats["still_empty"] >= 1
    # the figure-bearing row gained its figure
    figur = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'"
    ).fetchone()
    assert json.loads(figur["images_json"]) == figrow["images"]
    # the figure-less row stayed empty
    plain = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-plain%'"
    ).fetchone()
    assert plain["images_json"] == "[]"


def test_backfill_figures_hint_only_targets_stems_naming_assets(db, fig_dirs, monkeypatch):
    """`figure_hint=True` is the default and the only mode run by the CLI without
    --full-sweep. It limits the sweep to rows whose stem mentions a figure
    (graph/figure/diagram/table/chart/scatterplot/map/illustration/plot). Two
    imageless rows: one stem names a scatterplot, one says nothing. With the
    hint on, the sweep fetches only the named-assets row."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
    # named-assets row: figure-less in storage, stem mentions a "scatterplot"
    figrow = _normalize(DETAIL_FIG, FIG_META)
    insert_qbank_row(conn, figrow, batch="bfill-hint")
    conn.execute("UPDATE questions SET images_json='[]' WHERE provenance_json LIKE '%ext-fig%'")
    # plain row: no asset-language in stem, no figures
    conn.execute("""INSERT INTO questions (fingerprint, source, source_test, source_question_number,
                     module, passage, stem, choices_json, correct_letter, rationale, images_json,
                     official_domain, official_skill, skill_source, difficulty, pool,
                     seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
                   VALUES ('plain-hint', 'college_board_question_bank', 'ext-plain', '', '',
                     'A plain prompt.', 'Which completes the text?', '["one","two","three","four"]',
                     'A', '', '[]', '', 'Inferences', 'metadata', 'medium', 'fresh_training',
                     0, 1, 'old', '2026-01-01', '{"external_id":"ext-plain"}')""")
    conn.commit()

    def fake_fetch(ext):
        if str(ext) == "ext-fig":
            return DETAIL_FIG
        return DETAIL  # ext-plain: no figure (would also be skipped by the hint)

    monkeypatch.setattr(qbank_fetch, "fetch_question", fake_fetch)

    stats = qbank_fetch.backfill_figures(conn, figure_hint=True, sleep_s=0)

    # Only one candidate, and the plain row was excluded by the hint SQL.
    assert stats["candidate"] == 1
    assert stats["now_images"] == 1
    # the named-assets row gained its figure
    figur = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-fig%'"
    ).fetchone()
    assert json.loads(figur["images_json"]) == figrow["images"]
    # the plain row was never fetched and stayed empty
    plain = conn.execute(
        "SELECT images_json FROM questions WHERE provenance_json LIKE '%ext-plain%'"
    ).fetchone()
    assert plain["images_json"] == "[]"


def test_backfill_figures_respects_limit(db, fig_dirs, monkeypatch):
    """`--limit N` caps the backfill to N candidates even when more match. This
    pins the documented limit interaction: a fetch_qbank run with --limit 10
    also caps the backfill at 10 rows, and the limit slices the ORDER BY id
    ordering (not the SQL query), so the lowest-id candidates run first."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
    # three figure-bearing rows stored imageless, with distinct external_ids and
    # distinct passages so each inserts a fresh row (same content dedupes).
    for n, ext in enumerate(["ext-fig-a", "ext-fig-b", "ext-fig-c"], 1):
        meta = {
            "external_id": ext,
            "primary_class_cd_desc": "Information and Ideas",
            "skill_desc": "Inferences",
            "difficulty": "H",
        }
        detail = dict(DETAIL_FIG)
        detail["stimulus"] = f"<p>The scatterplot shows yield versus rainfall. Variant {n}.</p>"
        row = _normalize(detail, meta)
        insert_qbank_row(conn, row, batch="bfill-lim")
    conn.execute("UPDATE questions SET images_json='[]' WHERE source='college_board_question_bank'")
    conn.commit()

    fetched = []

    def fake_fetch(ext):
        fetched.append(str(ext))
        return DETAIL_FIG

    monkeypatch.setattr(qbank_fetch, "fetch_question", fake_fetch)

    stats = qbank_fetch.backfill_figures(conn, figure_hint=True, limit=2, sleep_s=0)

    # 2 candidates were processed (sliced from ORDER BY id), 1 was not.
    assert stats["candidate"] == 2
    assert stats["now_images"] == 2
    assert len(fetched) == 2
    # the untouched row still has no figures
    rows_with_imgs = conn.execute(
        "SELECT provenance_json FROM questions WHERE images_json != '[]'"
    ).fetchall()
    assert len(rows_with_imgs) == 2
