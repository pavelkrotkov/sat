"""Sanitized accessible visuals preserve order and legacy fingerprint text."""

import base64
import json

import pytest
from conftest import add_question

from satprep.corpus.qbank_fetch import (
    _extract_visuals,
    _normalize,
    insert_qbank_row,
    sanitize_table,
)
from tests.test_qbank_fetch import DETAIL, META

TABLE_HTML = (
    "<figure class='table'>"
    "<table>"
    "<caption>Table 1. Yield by season</caption>"
    "<thead><tr><th id='c1' scope='col'>Season</th><th id='c2' scope='col'>Yield (t/ha)</th></tr></thead>"
    "<tbody>"
    "<tr><td headers='c1'>Spring</td><td headers='c2'>3.1</td></tr>"
    "<tr><td headers='c1'>Summer</td><td headers='c2'>4.7</td></tr>"
    "</tbody>"
    "</table>"
    "</figure>"
)

SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<rect width='100' height='100'/>"
    "<text x='5' y='10'>yield (tons)</text></svg>"
)


@pytest.fixture()
def fig_dirs(tmp_path, monkeypatch):
    images = tmp_path / "images"
    images.mkdir()
    monkeypatch.setattr("satprep.corpus.qbank_fetch.FIGURE_DIR", images)
    return images


def test_normalize_preserves_table_as_visual(fig_dirs):
    """Acceptance: a table fixture is not flattened into indistinguishable
    prose; its caption, headers, rows and cell relationships survive as a
    first-class visual record."""
    detail = {
        **DETAIL,
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-table",
    }
    row = _normalize(detail, dict(META, external_id="ext-table"))

    tables = [v for v in row["visuals"] if v.get("kind") == "table"]
    assert len(tables) == 1
    html = tables[0]["html"]
    assert "<table>" in html and "<caption>Table 1. Yield by season</caption>" in html
    assert "<thead>" in html and "<tbody>" in html
    assert "Season" in html and "Yield (t/ha)" in html
    assert "Spring" in html and "3.1" in html
    # no images were persisted for a table-only question
    assert row["images"] == []
    # the flattened text fallback still reaches the passage (fingerprint
    # stability: the old ingest stored the same stripped text)
    assert "Yield by season" in row["passage"]
    assert "Spring" in row["passage"] and "3.1" in row["passage"]


def test_sanitize_table_drops_scripts_and_foreign_tags():
    """The stored table markup is rendered with |safe, so sanitization must
    be structural: no script/style/event/URL content and no foreign tags can
    survive."""
    evil = (
        "<table>"
        "<thead><tr><th scope='col'>A</th></tr></thead>"
        "<tbody><tr><td onmouseover='alert(1)' style='color:red'>"
        "<script>alert('xss')</script>"
        "<a href='javascript:alert(2)'>link</a>"
        "<img src=x onerror=alert(3)>"
        "cell"
        "</td></tr></tbody>"
        "</table>"
    )
    out = sanitize_table(evil)
    assert out is not None
    for banned in ("<script", "onmouseover", "style=", "javascript:", "<a ", "<img", "onerror"):
        assert banned not in out, f"{banned!r} survived sanitization"
    assert "cell" in out


def test_sanitize_table_keeps_only_table_structure():
    out = sanitize_table(TABLE_HTML)
    assert out is not None
    # every tag in the output is on the allowlist
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(out, "html.parser")
    for tag in soup.find_all(True):
        assert tag.name in {
            "table",
            "caption",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "th",
            "td",
            "col",
            "colgroup",
        }
        for attr in tag.attrs:
            assert attr in {"scope", "colspan", "rowspan", "headers", "id", "span"}


def test_sanitize_table_scope_is_an_enum():
    """Table scope accepts only the four enumerated HTML values."""
    out = sanitize_table(
        "<table><thead><tr><th scope='col'>H</th><th scope='xss' onmouseover='a'>J</th></tr></thead>"
        "<tbody><tr><td>v</td><td>w</td></tr></tbody></table>"
    )
    assert out is not None
    assert 'scope="col"' in out or "scope='col'" in out
    # the bogus scope and event attr are gone
    assert "xss" not in out and "onmouseover" not in out


def test_sanitize_table_rewrites_header_ids():
    """headers="..." references must resolve to ids INSIDE the persisted
    table; a source id that would collide or escape is rewritten."""
    out = sanitize_table(
        "<table><thead><tr><th id='x' scope='col'>H</th></tr></thead>"
        "<tbody><tr><td headers='x'>v</td></tr></tbody></table>"
    )
    assert out is not None
    assert "id='eqb-th-1'" in out or 'id="eqb-th-1"' in out
    assert "headers='eqb-th-1'" in out or 'headers="eqb-th-1"' in out
    # a dangling reference (unknown id) is dropped, not kept
    out2 = sanitize_table(
        "<table><thead><tr><th scope='col'>H</th></tr></thead>"
        "<tbody><tr><td headers='nope'>v</td></tr></tbody></table>"
    )
    assert out2 is not None
    assert "headers" not in out2


def test_sanitize_table_empty_cells_yields_none():
    assert sanitize_table("<table><tr><td></td></tr></table>") is None
    assert sanitize_table("<p>no table</p>") is None
    assert sanitize_table("") is None


def test_normalize_mixed_visuals_in_document_order(fig_dirs):
    """A table followed by an svg figure in the same field must both be
    preserved, in source order, with the figure saved to disk."""
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    detail = {
        **DETAIL,
        "stimulus": (
            f"<p>Intro.</p>{TABLE_HTML}"
            f"<figure>{SVG}</figure>"
            f'<img src="data:image/png;base64,{png}">'
        ),
        "externalid": "ext-mix",
    }
    row = _normalize(detail, dict(META, external_id="ext-mix"))

    kinds = [v.get("kind") for v in row["visuals"]]
    assert kinds == ["table", "image", "image"]
    files = [v["file"] for v in row["visuals"] if v.get("kind") == "image"]
    assert [f.rsplit(".", 1)[1] for f in files] == ["svg", "png"]
    assert row["images"] == files
    assert all((fig_dirs / f).exists() for f in files)


def test_multiple_tables_preserve_document_order(fig_dirs):
    """Each figure-wrapped table is extracted once in source order."""
    t2 = (
        "<figure class='table'><table><caption>Table B. Rainfall</caption>"
        "<thead><tr><th id='b1' scope='col'>Month</th></tr></thead>"
        "<tbody><tr><td headers='b1'>March</td></tr></tbody></table></figure>"
    )
    detail = {
        **DETAIL,
        "stimulus": f"<p>Two tables.</p>{TABLE_HTML}{t2}",
        "externalid": "ext-2tables",
    }
    row = _normalize(detail, dict(META, external_id="ext-2tables"))

    tables = [v for v in row["visuals"] if v.get("kind") == "table"]
    assert len(tables) == 2
    assert "Table 1. Yield by season" in tables[0]["html"]
    assert "Table B. Rainfall" in tables[1]["html"]
    assert "March" in tables[1]["html"] and "Spring" in tables[0]["html"]


def test_insert_persists_visuals_json(db, fig_dirs):
    conn, _path = db
    detail = {
        **DETAIL,
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-table",
    }
    row = _normalize(detail, dict(META, external_id="ext-table"))
    insert_qbank_row(conn, row, batch="b-tbl")

    r = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-table%'"
    ).fetchone()
    assert json.loads(r["visuals_json"]) == row["visuals"]


def test_reimport_backfills_missing_visuals(db, fig_dirs):
    """A bank row ingested before visual extraction has empty
    visuals_json; re-importing the same external_id must attach the table
    without creating a duplicate question."""
    from satprep.corpus.fingerprint import fingerprint

    conn, _path = db
    # pre-seed the same item as a visual-less row (as the old ingest left it)
    bare = {
        "passage": "The table shows yields.",
        "stem": "Which choice most effectively uses data from the table to complete the text?",
        "choices": [
            {"letter": ch, "text": t, "is_correct": ch == "B"}
            for ch, t in zip("ABCD", ["opt A", "opt B", "opt C", "opt D"], strict=False)
        ],
        "correct": "B",
        "difficulty": "hard",
        "skill": "Inferences",
        "domain": "Information and Ideas",
        "ext_id": "ext-table",
    }
    fp = fingerprint(bare["passage"], bare["stem"], [c["text"] for c in bare["choices"]])
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             visuals_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES (?, 'college_board_question_bank', 'ext-table', '', '',
             ?, ?, ?, 'B', '', '[]', '[]',
             '', 'Inferences', 'metadata', 'hard', 'fresh_training',
             0, 1, 'old', '2026-01-01', '{"external_id":"ext-table"}')""",
        (fp, bare["passage"], bare["stem"], json.dumps(bare["choices"])),
    )
    conn.commit()

    detail = {
        **DETAIL,
        "stem": "Which choice most effectively uses data from the table to complete the text?",
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-table",
    }
    row = _normalize(detail, dict(META, external_id="ext-table"))
    outcome = insert_qbank_row(conn, row, batch="b-tbl2")
    assert outcome == "duplicate"

    r = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-table%'"
    ).fetchone()
    assert json.loads(r["visuals_json"]) == row["visuals"]


def test_backfill_visuals_attaches_tables_to_visualless_rows(db, fig_dirs, monkeypatch):
    """The sweep must repair table questions stored before extraction, and
    leave genuinely visual-less rows alone."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
    detail = {
        **DETAIL,
        "stem": "Which choice most effectively uses data from the table to complete the text?",
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-table",
    }
    row = _normalize(detail, dict(META, external_id="ext-table"))
    insert_qbank_row(conn, row, batch="bfill-v")
    # simulate the pre-fix ingest that dropped the visuals
    conn.execute(
        "UPDATE questions SET images_json='[]', visuals_json='[]' "
        "WHERE provenance_json LIKE '%ext-table%'"
    )
    conn.execute(
        """INSERT INTO questions (fingerprint, source, source_test, source_question_number,
             module, passage, stem, choices_json, correct_letter, rationale, images_json,
             visuals_json,
             official_domain, official_skill, skill_source, difficulty, pool,
             seen_benchmark, is_new_bank, import_batch, imported_at, provenance_json)
           VALUES ('plain-v', 'college_board_question_bank', 'ext-plain', '', '',
             'A plain prompt.', 'Which completes the text?', '["one","two","three","four"]',
             'A', '', '[]', '[]', '', 'Inferences', 'metadata', 'medium', 'fresh_training',
             0, 1, 'old', '2026-01-01', '{"external_id":"ext-plain"}')"""
    )
    conn.commit()

    def fake_fetch(ext):
        if str(ext) == "ext-table":
            return detail
        return DETAIL

    monkeypatch.setattr(qbank_fetch, "fetch_question", fake_fetch)
    stats = qbank_fetch.backfill_visuals(conn, hint=True, sleep_s=0)

    assert stats["now_visuals"] == 1
    assert stats["now_tables"] == 1
    fixed = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-table%'"
    ).fetchone()
    assert len(json.loads(fixed["visuals_json"])) == 1
    plain = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-plain%'"
    ).fetchone()
    assert plain["visuals_json"] == "[]"


def test_backfill_audit_reports_without_writing(db, fig_dirs, monkeypatch):
    """The acceptance-criteria audit: a read-only sweep reports what WOULD
    change (including missing files) and writes nothing."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
    detail = {
        **DETAIL,
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-table-audit",
    }
    row = _normalize(detail, dict(META, external_id="ext-table-audit"))
    insert_qbank_row(conn, row, batch="baudit")
    conn.execute(
        "UPDATE questions SET images_json='[]', visuals_json='[]' "
        "WHERE provenance_json LIKE '%ext-table-audit%'"
    )
    conn.commit()

    monkeypatch.setattr(qbank_fetch, "fetch_question", lambda ext: detail)
    before = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-table-audit%'"
    ).fetchone()["visuals_json"]

    # hint=False: the generic stem carries no visual keyword, so the hint
    # filter would exclude the only candidate; the audit sweeps everything.
    stats = qbank_fetch.backfill_visuals(conn, hint=False, sleep_s=0, audit_only=True)

    # The audit reports preservable visuals without writing them.
    assert stats["now_visuals"] == 1
    assert stats["candidate"] >= 1
    after = conn.execute(
        "SELECT visuals_json FROM questions WHERE provenance_json LIKE '%ext-table-audit%'"
    ).fetchone()["visuals_json"]
    assert after == before == "[]"


def test_visuals_survive_archive_round_trip(db, tmp_path):
    from satprep.corpus.archive import export_corpus, restore_corpus

    conn, _path = db
    qid = add_question(conn, passage="p", stem="s?", choices=["a", "b", "c", "d"])
    conn.execute(
        "UPDATE questions SET visuals_json=? WHERE id=?",
        (
            json.dumps(
                [
                    {"kind": "table", "html": "<table><tr><td>x</td></tr></table>"},
                    {"kind": "image", "file": "eqb-1-figure-1.svg"},
                ]
            ),
            qid,
        ),
    )
    conn.commit()
    out = tmp_path / "corpus.jsonl"
    export_corpus(conn, out)
    rec = json.loads(out.read_text().splitlines()[0])
    assert rec["visuals"][0]["kind"] == "table"
    assert rec["visuals"][1]["file"].endswith(".svg")

    fresh = tmp_path / "fresh.db"
    conn2 = __import__("satprep.db", fromlist=["connect"]).connect(str(fresh))
    try:
        restore_corpus(conn2, out)
        conn2.commit()
        row = conn2.execute("SELECT visuals_json FROM questions").fetchone()
        assert json.loads(row["visuals_json"])[0]["kind"] == "table"
    finally:
        conn2.close()


def test_drill_page_renders_a_table_visual(db, monkeypatch):
    """Acceptance: the drill template must render a persisted table, not
    flatten it. The table's caption/headers/cells appear in the HTML and no
    foreign markup from the source survives."""
    from satprep import server as server_mod
    from satprep.db import db_context
    from satprep.training.sessions import create_session

    conn, path = db
    qid = add_question(
        conn,
        passage="The table shows yields.",
        stem="Which choice completes the text?",
        choices=["a", "b", "c", "d"],
        source="bluebook_test",
        pool="historical",
    )
    conn.execute(
        "UPDATE questions SET visuals_json=? WHERE id=?",
        (json.dumps([{"kind": "table", "html": TABLE_HTML}]), qid),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server_mod, "db_context", lambda db_path=None: db_context(path))
    with db_context(path) as c:
        sess = create_session(c, "error_clinic", count=2, seed="vis-render")
    sid = sess["plan"]["session_id"]

    with db_context(path) as c:
        # the first question in the plan is the one we seeded
        response = server_mod.question(None, sid, 0, conn=c)

    html = response.body.decode()
    assert response.status_code == 200
    assert "Table 1. Yield by season" in html  # caption survives
    assert "<table>" in html and "<thead>" in html
    assert "Spring" in html and "3.1" in html
    # the persisted visual must not smuggle in inline scripting (the base
    # template's own static <script src> tag is expected and excluded)
    start = html.index("<caption>")
    end = html.index("</table>") + len("</table>")
    table_region = html[start:end]
    assert "<script" not in table_region
    assert "onmouseover" not in table_region and "javascript:" not in table_region


def test_extract_visuals_table_text_fallback_preserves_fingerprint(db, fig_dirs):
    """The cleaned text for a table question must match what the pre-#46
    ingest stored (all tags stripped, cells flattened), so fingerprints and
    legacy rows do not drift."""
    from satprep.corpus import qbank_fetch
    from satprep.corpus.fingerprint import fingerprint

    html = f"<p>The table shows yields.</p>{TABLE_HTML}"
    remaining, visuals, _, _ = _extract_visuals("ext-fp", html, fig_dirs)
    assert len(visuals) == 1
    # all table markup gone; flattened text present
    assert "<table" not in remaining and "<caption" not in remaining
    assert "The table shows yields." in remaining
    assert "Table 1. Yield by season" in remaining
    assert "Spring" in remaining and "3.1" in remaining

    legacy = (
        "The table shows yields. Table 1. Yield by season Season Yield (t/ha) Spring 3.1 Summer 4.7"
    )
    # _normalize applies _clean_html to the remaining html before hashing;
    # mirror that here so the comparison is apples-to-apples.
    cleaned = qbank_fetch._clean_html(remaining)
    assert fingerprint(cleaned, "", []) == fingerprint(legacy, "", [])


def test_restore_sanitizes_table_visuals():
    """Archive restore sanitizes table HTML before it reaches a safe-rendered field."""
    from satprep.corpus.archive import _restore_visuals

    cleaned = _restore_visuals(
        [
            {
                "kind": "table",
                "html": "<table onclick='alert(1)'><tr><td>"
                "<script>evil()</script>cell</td></tr></table>",
            },
            {"kind": "image", "file": "eqb-1-figure-1.svg"},
            {"kind": "bogus", "html": "<script>x</script>"},
            {"kind": "image", "file": ""},
        ]
    )
    assert len(cleaned) == 2
    assert cleaned[0]["kind"] == "table"
    assert "alert" not in cleaned[0]["html"] and "<script" not in cleaned[0]["html"]
    assert "cell" in cleaned[0]["html"]
    assert cleaned[1] == {"kind": "image", "file": "eqb-1-figure-1.svg"}


def test_backfill_skips_complete_table_only_rows(db, fig_dirs):
    """A complete table-only row legitimately has no image files."""
    conn, _path = db
    detail = {
        **DETAIL,
        "stem": "Which choice completes the text?",
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-complete",
    }
    row = _normalize(detail, dict(META, external_id="ext-complete"))
    insert_qbank_row(conn, row, batch="b-complete")
    conn.commit()

    rows = conn.execute(
        "SELECT provenance_json FROM questions WHERE source='college_board_question_bank'"
        " AND active=1 AND visuals_json IN ('[]','','null')"
    ).fetchall()
    assert not any("ext-complete" in (r["provenance_json"] or "") for r in rows), (
        "complete table-only row re-selected by the backfill predicate"
    )


def test_audit_counts_preserved_visuals(db, fig_dirs, monkeypatch):
    """A read-only visual audit reports what would be preserved."""
    from satprep.corpus import qbank_fetch

    conn, _path = db
    detail = {
        **DETAIL,
        "stimulus": f"<p>The table shows yields.</p>{TABLE_HTML}",
        "externalid": "ext-audit-count",
    }
    row = _normalize(detail, dict(META, external_id="ext-audit-count"))
    insert_qbank_row(conn, row, batch="b-ac")
    conn.execute(
        "UPDATE questions SET visuals_json='[]' WHERE provenance_json LIKE '%ext-audit-count%'"
    )
    conn.commit()

    monkeypatch.setattr(qbank_fetch, "fetch_question", lambda ext: detail)
    stats = qbank_fetch.backfill_visuals(conn, hint=False, sleep_s=0, audit_only=True)
    assert stats["now_visuals"] == 1
    assert stats["now_tables"] == 1


def test_table_wins_over_nested_svg(fig_dirs):
    """A table wrapper owns nested SVG and remains a table visual."""
    svg_cell = (
        "<figure class='table'><table><caption>Table S. Data</caption>"
        "<thead><tr><th scope='col'>A</th><th scope='col'>B</th></tr></thead>"
        f"<tbody><tr><td>{SVG}</td><td>1.0</td></tr></tbody></table></figure>"
    )
    detail = {
        **DETAIL,
        "stimulus": svg_cell,
        "externalid": "ext-svgcell",
    }
    row = _normalize(detail, dict(META, external_id="ext-svgcell"))
    tables = [v for v in row["visuals"] if v.get("kind") == "table"]
    assert len(tables) == 1, f"expected a table visual, got {row['visuals']}"
    assert "Table S. Data" in tables[0]["html"]
    assert "1.0" in tables[0]["html"]
    assert not any(v.get("kind") == "image" for v in row["visuals"])


def test_figure_with_multiple_data_uri_images_saves_all(fig_dirs):
    """Every data-URI image in a figure is persisted."""
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    png2 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02").decode()
    detail = {
        **DETAIL,
        "stimulus": (
            f'<figure><img src="data:image/png;base64,{png}">'
            f'<img src="data:image/png;base64,{png2}"></figure>'
        ),
        "externalid": "ext-multi-img",
    }
    row = _normalize(detail, dict(META, external_id="ext-multi-img"))
    images = [v for v in row["visuals"] if v.get("kind") == "image"]
    assert len(images) == 2, f"expected both data-URI images, got {row['visuals']}"
    files = [v["file"] for v in images]
    assert len(set(files)) == 2
    for f in files:
        assert (fig_dirs / f).exists()


def test_sanitize_table_turns_break_into_separator():
    """A table line break preserves separation between cell values."""
    from bs4 import BeautifulSoup

    out = sanitize_table("<table><tr><td>1<br>2</td></tr></table>")
    assert out is not None
    text = BeautifulSoup(out, "html.parser").get_text()
    # the two numbers are kept apart by whitespace, not fused into "12"
    assert "1" in text and "2" in text and "12" not in text
