"""Tests for the deterministic KB lint script (issue #39)."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_kb.py"


def _run(*args: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# End-to-end against the real repo: must be clean (this is the gate).
# ---------------------------------------------------------------------------


def test_real_vault_lints_clean():
    r = _run("--check")
    assert r.returncode == 0, (
        "scripts/check_kb.py --check must pass against the committed KB.\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )


def test_real_vault_writes_deterministic_index():
    r1 = _run()
    assert r1.returncode == 0
    idx1 = (REPO_ROOT / "kb" / ".kb-index.json").read_bytes()
    r2 = _run()
    assert r2.returncode == 0
    idx2 = (REPO_ROOT / "kb" / ".kb-index.json").read_bytes()
    assert idx1 == idx2, "index output must be deterministic across runs"


def test_index_has_required_shape():
    r = _run()
    assert r.returncode == 0
    idx = json.loads((REPO_ROOT / "kb" / ".kb-index.json").read_text())
    assert idx["schema_version"] == 1
    assert idx["vault"] == "kb/wiki"
    assert isinstance(idx["sources"], list) and len(idx["sources"]) == 6
    # 7 indexed pages: 1 concept + 6 summaries. The lint script intentionally
    # excludes review-templates/ and reports/ (see TEMPLATE_DIRS), and
    # reviews/README.md has no frontmatter so it lands in the bare-file
    # branch and is not indexed either (the index is for retrieval by type).
    assert isinstance(idx["pages"], list) and len(idx["pages"]) == 7
    types = {p["type"] for p in idx["pages"]}
    assert types == {"concept", "summary"}
    for p in idx["pages"]:
        for k in ("path", "type", "title", "tags", "sources", "wikilinks", "question_fingerprint"):
            assert k in p, f"missing key {k} in {p.get('path')}"


# ---------------------------------------------------------------------------
# Negative cases: run the script in a temp copy of the vault and verify each
# finding class surfaces an error and a non-zero exit.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_vault(tmp_path) -> pathlib.Path:
    """Copy the real vault into a tempdir so we can break it without touching
    the working tree. We deliberately keep the .openkb/ runtime directory and
    kb/build/ out of the copy — neither is required for linting."""
    import shutil

    src = REPO_ROOT / "kb"
    dst = tmp_path / "kb"
    shutil.copytree(
        src, dst, ignore=shutil.ignore_patterns("build", ".openkb", "wiki" + os.sep + "reports")
    )
    # check_kb.py resolves the repo root from a sentinel (kb/wiki/index.md).
    # tmp_path/kb/wiki/index.md exists, so the script will treat tmp_path as
    # the repo root. Good.
    return dst


def _run_in(vault_root: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    # Run the script with SAT_KB_ROOT pointed at the temp vault so the
    # script's find_repo_root() uses the broken copy, not the real repo.
    import os

    env = {**os.environ, "SAT_KB_ROOT": str(vault_root.parent)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=vault_root.parent,
        env=env,
    )


def test_missing_required_frontmatter_blocks(tmp_vault):
    p = tmp_vault / "wiki" / "summaries" / "broken-summary.md"
    p.write_text(
        "---\n"
        "title: Broken\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        # missing tags, sources, confidence
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "FM_REQUIRED" in r.stderr
    assert "tags" in r.stderr and "sources" in r.stderr and "confidence" in r.stderr


def test_unknown_type_blocks(tmp_vault):
    p = tmp_vault / "wiki" / "summaries" / "weird-type.md"
    p.write_text(
        "---\n"
        "title: Weird\n"
        "type: not-a-real-type\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [t]\n"
        "sources: [raw/transcripts/youtube-HlkBuNW-VHE.txt]\n"
        "confidence: low\n"
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "FM_TYPE_UNKNOWN" in r.stderr


def test_invalid_question_fingerprint_blocks(tmp_vault):
    p = tmp_vault / "wiki" / "reviews" / "broken-review.md"
    p.write_text(
        "---\n"
        "title: Bad FP\n"
        "type: question-review\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [t]\n"
        "question_fingerprint: not-a-sha\n"
        "student_answer: A\n"
        "correct_answer: B\n"
        "confidence: high\n"
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "FM_FINGERPRINT" in r.stderr


def test_broken_wikilink_blocks(tmp_vault):
    p = tmp_vault / "wiki" / "summaries" / "broken-links.md"
    p.write_text(
        "---\n"
        "title: Broken links\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [t]\n"
        "sources: [transcripts/youtube-HlkBuNW-VHE.txt]\n"
        "confidence: low\n"
        "---\n\nSee [[nope/this-page-doesnt-exist]].\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "WIKILINK" in r.stderr
    assert "nope/this-page-doesnt-exist" in r.stderr


def test_manifest_sha_mismatch_blocks(tmp_vault):
    manifest = tmp_vault / "raw" / "source-manifest.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    # Corrupt the sha of the first row.
    rows[0]["sha256"] = "0" * 64
    manifest.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "MANIFEST_SHA" in r.stderr


def test_manifest_missing_trailing_newline_blocks(tmp_vault):
    manifest = tmp_vault / "raw" / "source-manifest.jsonl"
    text = manifest.read_text()
    if text.endswith("\n"):
        manifest.write_text(text[:-1])
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "MANIFEST_NEWLINE" in r.stderr


def test_duplicate_question_fingerprint_blocks(tmp_vault):
    fp = "a" * 64
    # Pre-seed two reviews with the same fingerprint.
    for i, name in enumerate(["review-1.md", "review-2.md"]):
        p = tmp_vault / "wiki" / "reviews" / name
        p.write_text(
            f"---\n"
            f"title: R{i}\n"
            f"type: question-review\n"
            f"created: 2026-01-01\n"
            f"updated: 2026-01-01\n"
            f"tags: [t]\n"
            f"question_fingerprint: {fp}\n"
            f"student_answer: A\n"
            f"correct_answer: B\n"
            f"confidence: high\n"
            f"---\n\nbody {i}\n"
        )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "REVIEW_DUP" in r.stderr


def test_orphaned_transcript_warns_only(tmp_vault):
    # Add a manifest row whose transcript is not cited by any page.
    transcript = tmp_vault / "raw" / "transcripts" / "youtube-orphan.txt"
    transcript.write_text("orphan content\n")
    manifest = tmp_vault / "raw" / "source-manifest.jsonl"
    extra = json.dumps(
        {
            "source_id": "s-orphan",
            "title": "Orphan",
            "url": "https://example.com/orphan",
            "retrieved_at": "2026-01-01T00:00:00+00:00",
            "content_type": "text/plain",
            "sha256": hashlib_sha256(b"orphan content\n"),
            "bytes": 15,
            "authority": "unofficial",
            "transcript": "transcripts/youtube-orphan.txt",
        }
    )
    manifest.write_text(manifest.read_text() + extra + "\n")
    # Drop the committed index in the temp copy so the test doesn't fail
    # the (separately tested) INDEX_STALE path.
    (tmp_vault / ".kb-index.json").unlink(missing_ok=True)
    r = _run_in(tmp_vault, "--check")
    # warning, not error: lint still passes for the orphan itself; we
    # pre-deleted the committed index so INDEX_MISSING would also fire
    # and that path is tested separately. Avoid the false flag here.
    assert "TRANSCRIPT_ORPHAN" in r.stderr
    if r.returncode != 0:
        # INDEX_MISSING is the expected additional error; everything
        # else should be a warning, not a hard error.
        non_warnings = [
            line
            for line in r.stderr.splitlines()
            if line.startswith("ERROR") and "INDEX_MISSING" not in line
        ]
        assert not non_warnings, r.stderr


def test_nav_sections_blocks(tmp_vault):
    # Replace index.md with one that drops the "Summaries" section.
    (tmp_vault / "wiki" / "index.md").write_text("# Empty\n\n## Concepts\n- nothing\n")
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "NAV_SECTION" in r.stderr


def test_json_output_is_well_formed():
    r = _run("--json")
    assert r.returncode == 0
    findings = json.loads(r.stdout)
    assert isinstance(findings, list)
    for f in findings:
        assert set(f.keys()) == {"level", "path", "code", "message"}


def test_scalar_sources_is_rejected(tmp_vault):
    """A common YAML mistake is `sources: raw/transcripts/foo.txt` instead of
    a list. The lint must surface it explicitly rather than silently drop."""
    p = tmp_vault / "wiki" / "summaries" / "scalar-sources.md"
    p.write_text(
        "---\n"
        "title: Scalar\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [t]\n"
        "sources: raw/transcripts/youtube-HlkBuNW-VHE.txt\n"  # string, not list
        "confidence: low\n"
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "SOURCE_SCALAR" in r.stderr


def test_non_mapping_frontmatter_is_rejected(tmp_vault):
    """Frontmatter that parses to a list/scalar (not a mapping) must be
    rejected with a clear code rather than crashing."""
    p = tmp_vault / "wiki" / "summaries" / "list-fm.md"
    p.write_text("---\n- just\n- a\n- list\n---\n\nbody\n")
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "FM_NOT_MAPPING" in r.stderr


def test_path_traversal_in_sources_is_rejected(tmp_vault):
    """A `sources:` entry that resolves outside kb/raw/ is a path-traversal
    attempt (e.g. a symlink targeting /home/pavel/.ssh). Surface as an
    error, not a silent accept."""
    p = tmp_vault / "wiki" / "summaries" / "traversal-sources.md"
    # ../../etc/passwd resolves to <tmp>/etc/passwd which is outside RAW.
    p.write_text(
        "---\n"
        "title: traversal\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [t]\n"
        "sources: ['../../etc/passwd']\n"
        "confidence: low\n"
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "SOURCE_TRAVERSAL" in r.stderr


def test_stale_committed_index_blocks_check(tmp_vault):
    """`--check` mode must fail if the committed kb/.kb-index.json is stale
    relative to what would be regenerated from the current vault. CI relies
    on this gate to prevent a drift between the index and the data it
    describes."""
    # The fixture's committed index is byte-identical to the regeneration
    # right after copy, so we need to mutate the vault to force drift.
    p = tmp_vault / "wiki" / "summaries" / "drift-summaries" / "new.md"
    p.parent.mkdir(parents=True)
    p.write_text(
        "---\n"
        "title: Drift\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [drift]\n"
        "sources: [transcripts/youtube-HlkBuNW-VHE.txt]\n"
        "confidence: low\n"
        "---\n\nbody\n"
    )
    # NOTE: the new file lives under summaries/ which discover_md() walks
    # via .rglob, but rglob doesn't follow into the deep new file. Build
    # a flat file alongside the existing summaries instead.
    p.unlink()
    p2 = tmp_vault / "wiki" / "summaries" / "drift-summaries.md"
    p2.write_text(
        "---\n"
        "title: Drift\n"
        "type: summary\n"
        "created: 2026-01-01\n"
        "updated: 2026-01-01\n"
        "tags: [drift]\n"
        "sources: [transcripts/youtube-HlkBuNW-VHE.txt]\n"
        "confidence: low\n"
        "---\n\nbody\n"
    )
    r = _run_in(tmp_vault, "--check")
    assert r.returncode == 1
    assert "INDEX_STALE" in r.stderr


def test_rebuild_refuses_vault_symlinks(tmp_path):
    """kb/rebuild.sh must refuse to stage a vault that contains any symlink,
    because `cp -R` (or any dereferencing variant) could otherwise copy a
    symlink target outside the vault into the rendered site. A symlink whose
    target is a real file inside the vault is also rejected: the vault is
    content, not a graph of links."""
    import shutil

    src = REPO_ROOT / "kb"
    dst = tmp_path / "kb"
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(
            "build", ".openkb", "wiki" + os.sep + "reports", ".kb-index.json"
        ),
    )
    # rebuild.sh runs scripts/check_kb.py from $REPO/scripts/. Copy the
    # lint script alongside the temp kb/ so the wrapper can find it.
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / "scripts" / "check_kb.py", tmp_path / "scripts" / "check_kb.py")
    # Plant a symlink pointing at an outside file. /etc/hostname is a safe,
    # tiny, real file on every Linux box; the test just needs any file that
    # is NOT under kb/.
    (dst / "wiki" / "summaries" / "evil.md").symlink_to("/etc/hostname")
    # Point the rebuild wrapper's defaults at our temp vault and a fake
    # builder so the script runs far enough to reach the symlink rejection.
    fake_builder = tmp_path / "fake_build_wiki.py"
    fake_builder.write_text("import sys\nsys.exit(0)\n")
    env = {
        **os.environ,
        "SAT_KB_ROOT": str(tmp_path),
        "SAT_WIKI_BUILDER": str(fake_builder),
        "SAT_WIKI_MKDOCS": "/bin/true",
    }
    r = subprocess.run(
        [str(REPO_ROOT / "kb" / "rebuild.sh")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert r.returncode != 0
    assert "symlink" in r.stderr.lower()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hashlib_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
