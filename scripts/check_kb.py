#!/usr/bin/env python3
"""Deterministic validator + retrieval-index generator for the SAT KB.

Implements the deterministic checks called out in issue #39:
  - required frontmatter by page type
  - manifest rows point to committed raw sources (sha256, bytes) and
    required provenance fields are well formed
  - broken wikilinks, missing transcript references, duplicate stable
    review IDs, orphaned authored pages
  - generated MkDocs nav includes concepts, summaries, templates, reviews
  - emits a deterministic retrieval index for KB-aware explanations
    (consumed by the LLM pipeline in #36)

Pure stdlib + PyYAML (already a transitive dep of MkDocs). No network,
no controller-host paths, no LLM provider. Exit code 0 = clean; 1 =
at least one finding; 2 = usage/IO error.

Usage:
  scripts/check_kb.py              # default: validate + regenerate index
  scripts/check_kb.py --check      # validate only; do not write index
  scripts/check_kb.py --index-out PATH
                                   # write retrieval index to PATH instead of
                                   # the default kb/.kb-index.json
  scripts/check_kb.py --json       # emit findings as JSON (still human-readable
                                   # when --quiet not set)

Output goes to stderr (findings) and stdout (the retrieval index when
regenerated). Local-only; the CI workflow runs the same command and fails
on any non-empty findings list.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("error: PyYAML is required (transitive dep of MkDocs)", file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# REPO_ROOT is overridable via SAT_KB_ROOT for tests and unusual layouts.
# All other paths are derived from it, so a single override re-roots the
# whole script.
REPO_ROOT = pathlib.Path(
    os.environ.get("SAT_KB_ROOT") or pathlib.Path(__file__).resolve().parent.parent
).resolve()
VAULT = REPO_ROOT / "kb" / "wiki"
RAW = REPO_ROOT / "kb" / "raw"
MANIFEST = RAW / "source-manifest.jsonl"
DEFAULT_INDEX = REPO_ROOT / "kb" / ".kb-index.json"

# Per-type required frontmatter. Every authored page also needs a `title`.
# `created` and `updated` are required for non-README pages because they
# feed KB log/history and the retrieval index timestamp.
REQUIRED_BY_TYPE: dict[str, list[str]] = {
    "summary": ["title", "type", "created", "updated", "tags", "sources", "confidence"],
    "concept": ["title", "type", "created", "updated", "tags", "sources", "confidence"],
    "question-review": [
        "title",
        "type",
        "created",
        "updated",
        "tags",
        "question_fingerprint",
        "student_answer",
        "correct_answer",
        "confidence",
    ],
}
ALLOWED_TYPES = set(REQUIRED_BY_TYPE) | {"log", "index", "readme"}

# PR-43 round-3: accept (and discard) an optional `#fragment` after the
# target, so a link such as [[concepts#Procedure]] matches this regex
# and is validated against its page target.
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Bare `index.md` is the nav hub and intentionally has no frontmatter; it is
# still a "page" we include in the index. `README.md` is also allowed (used
# in `reviews/` to host the authoring convention).
BARE_FILES = {"index.md", "README.md", "log.md"}
# PR-43 review: scope the "bare pages are exempt from frontmatter"
# exemption to the vault-relative paths where the convention is
# actually intended. Originally the exemption was based only on the
# basename, so any authored page named README.md / index.md / log.md
# anywhere under the vault silently bypassed the required-frontmatter
# gate. Compare vault-relative paths only.
_BARE_FILE_PATHS: set[str] = {
    # nav hub
    "kb/wiki/index.md",
    # top-level log
    "kb/wiki/log.md",
}
# A handful of topical README files are also bare by convention. Add
# them explicitly here rather than letting the basename rule exempt
# every matching filename under any directory.
_BARE_FILE_PATHS.update(
    {
        f.as_posix()
        for f in [
            pathlib.Path("kb/wiki/reviews/README.md"),
        ]
        if f.exists() or True  # declared; rely on file presence to swallow
    }
)
# The review-templates directory contains the authoring convention itself.
# Linting its frontmatter is misleading: the placeholder values (literal
# `64-hex-sha256`, `YYYY-MM-DD`, example tags) are intentional teaching
# material, not data. Skip the whole directory.
# kb/wiki/reports/ contains generated lint reports (gitignored); it would
# never pass the FM checks and is never the input we care about.
TEMPLATE_DIRS = {("review-templates",), ("reports",)}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Finding:
    level: str  # "error" | "warning"
    path: str  # repo-relative, forward-slash
    code: str  # short identifier for filtering
    message: str

    def render(self) -> str:
        return f"{self.level.upper():7s} {self.path} [{self.code}] {self.message}"


def _under_raw(p: pathlib.Path) -> bool:
    """True iff p.resolve() is the same file as, or a child of, RAW.

    `pathlib.Path.is_relative_to` is available on 3.9+; this wrapper
    tolerates the case where `resolve()` follows a symlink outside RAW
    (the resolve+relative_to pair catches that)."""
    try:
        p.resolve().relative_to(RAW.resolve())
    except ValueError:
        return False
    return True


def find_repo_root(start: pathlib.Path) -> pathlib.Path:
    return REPO_ROOT


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict, str | None]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, "missing frontmatter"
    try:
        return yaml.safe_load(m.group(1)) or {}, None
    except yaml.YAMLError as e:
        return {}, f"invalid YAML: {e}"


def _check_question_frontmatter(rel: pathlib.Path, fm: dict, findings: list[Finding]) -> None:
    if fm.get("type") != "question-review":
        return
    fp = fm.get("question_fingerprint", "")
    if not isinstance(fp, str) or not SHA256_RE.match(fp):
        findings.append(
            Finding(
                "error",
                rel.as_posix(),
                "FM_FINGERPRINT",
                "question_fingerprint must be a 64-char lowercase hex SHA-256",
            )
        )
    for answer in ("student_answer", "correct_answer"):
        value = fm.get(answer)
        if value not in {"A", "B", "C", "D", "E"}:
            findings.append(
                Finding(
                    "error",
                    rel.as_posix(),
                    "FM_ANSWER",
                    f"{answer} must be a single letter A-E, got {value!r}",
                )
            )


def check_frontmatter(rel: pathlib.Path, text: str) -> tuple[dict, list[Finding]]:
    """Return (parsed-frontmatter, findings). An empty dict with a finding
    means the frontmatter could not be parsed; callers should treat the
    empty dict as 'no FM' and continue with shape checks suppressed."""
    findings: list[Finding] = []
    # PR-43 review: exempt only the vault-relative paths that are
    # actually intended to be bare (the nav hub and a few topical
    # README files), not every file whose basename happens to match.
    rel_str = rel.as_posix()
    if rel_str in _BARE_FILE_PATHS:
        return {}, findings
    fm, err = parse_frontmatter(text)
    if err:
        findings.append(Finding("error", rel.as_posix(), "FM_MISSING", err))
        return {}, findings
    # YAML may parse to a non-mapping (a list, a scalar) at the top level.
    # Reject it explicitly so callers don't blow up on fm["type"].
    if not isinstance(fm, dict):
        findings.append(
            Finding(
                "error",
                rel.as_posix(),
                "FM_NOT_MAPPING",
                f"frontmatter must be a YAML mapping, got {type(fm).__name__}",
            )
        )
        return {}, findings
    if "type" not in fm:
        findings.append(Finding("error", rel.as_posix(), "FM_TYPE", "frontmatter missing `type`"))
        return fm, findings
    if fm["type"] not in ALLOWED_TYPES:
        findings.append(
            Finding(
                "error",
                rel.as_posix(),
                "FM_TYPE_UNKNOWN",
                f"unknown type {fm['type']!r}; expected one of {sorted(ALLOWED_TYPES)}",
            )
        )
        return fm, findings
    required = REQUIRED_BY_TYPE.get(fm["type"], ["title", "type"])
    for key in required:
        if key not in fm or fm[key] in (None, "", []):
            findings.append(
                Finding(
                    "error",
                    rel.as_posix(),
                    "FM_REQUIRED",
                    f"missing required field `{key}` for type={fm['type']}",
                )
            )
    _check_question_frontmatter(rel, fm, findings)
    return fm, findings


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _check_manifest_transcript(row: dict, lineno: int, findings: list[Finding]) -> None:
    rel = str(row.get("transcript", ""))
    if not rel:
        return
    path = (RAW / rel).resolve()
    try:
        path.relative_to(RAW)
    except ValueError:
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_TRAVERSAL",
                f"line {lineno}: transcript resolves outside kb/raw/: {rel}",
            )
        )
    if not path.is_file():
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_PATH",
                f"line {lineno}: transcript not found: {rel}",
            )
        )
        return
    actual = path.read_bytes()
    if hashlib.sha256(actual).hexdigest() != row.get("sha256"):
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_SHA",
                f"line {lineno}: sha256 mismatch for {rel}",
            )
        )
    try:
        want_bytes = int(row.get("bytes", -1))
    except (TypeError, ValueError):
        want_bytes = -1
    if len(actual) != want_bytes:
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_BYTES",
                f"line {lineno}: bytes mismatch for {rel} (manifest {want_bytes}, actual {len(actual)})",
            )
        )


def _check_manifest_row(
    row: object, lineno: int, seen_ids: set[str], findings: list[Finding]
) -> dict | None:
    path = "kb/raw/source-manifest.jsonl"
    if not isinstance(row, dict):
        findings.append(
            Finding(
                "error",
                path,
                "MANIFEST_ROW_SHAPE",
                f"line {lineno}: each manifest row must be a JSON object, got {type(row).__name__}",
            )
        )
        return None
    source_id = row.get("source_id", "")
    if not source_id:
        findings.append(Finding("error", path, "MANIFEST_ID", f"line {lineno}: missing source_id"))
    elif source_id in seen_ids:
        findings.append(
            Finding(
                "error", path, "MANIFEST_DUP", f"line {lineno}: duplicate source_id {source_id}"
            )
        )
    seen_ids.add(source_id)
    for field in (
        "title",
        "url",
        "retrieved_at",
        "content_type",
        "sha256",
        "bytes",
        "transcript",
        "authority",
    ):
        if field not in row or row[field] in (None, ""):
            findings.append(
                Finding("error", path, "MANIFEST_FIELD", f"line {lineno}: missing field {field}")
            )
    if not SHA256_RE.match(str(row.get("sha256", ""))):
        findings.append(
            Finding(
                "error",
                path,
                "MANIFEST_SHA",
                f"line {lineno}: sha256 must be a 64-char lowercase hex string",
            )
        )
    _check_manifest_transcript(row, lineno, findings)
    return row


def check_manifest(findings: list[Finding]) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    if not MANIFEST.exists():
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_MISSING",
                "source manifest is required",
            )
        )
        return rows
    text = MANIFEST.read_text(encoding="utf-8")
    # JSONL spec: each line must end with \n including the last; a missing
    # trailing newline is a finding (and a parser hazard for strict readers).
    if text and not text.endswith("\n"):
        findings.append(
            Finding(
                "error",
                "kb/raw/source-manifest.jsonl",
                "MANIFEST_NEWLINE",
                "missing trailing newline (JSONL spec)",
            )
        )
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as e:
            findings.append(
                Finding(
                    "error", "kb/raw/source-manifest.jsonl", "MANIFEST_JSON", f"line {lineno}: {e}"
                )
            )
            continue
        row = _check_manifest_row(decoded, lineno, seen_ids, findings)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Wikilinks + cross-references
# ---------------------------------------------------------------------------


def discover_md(root: pathlib.Path) -> list[pathlib.Path]:
    """Pages to validate and include in the retrieval index.

    Excludes any file under a generated (reports/) or template
    (review-templates/) subtree at any depth. The previous check
    compared the *complete* parent-parts tuple for equality, so it
    skipped `reports/foo.md` but not `reports/2026/foo.md`. The
    PR-43 fix: test the first relative path component instead.
    """
    pages = sorted(root.rglob("*.md"))
    out = []
    for p in pages:
        rel = pathlib.Path(p).relative_to(root)
        if rel.parts and rel.parts[0] in {"reports", "review-templates"}:
            continue
        out.append(p)
    return out


def _all_markdown_for_link_resolution(root: pathlib.Path) -> list[pathlib.Path]:
    """Every .md under the vault, including templates and generated
    reports. Wikilinks can resolve to any of these; we just don't put
    them in the retrieval index or run frontmatter checks on them.
    """
    return sorted(root.rglob("*.md"))


def _link_targets(all_pages: list[pathlib.Path]) -> set[str]:
    targets: set[str] = set()
    for page in all_pages:
        rel_to_vault = page.relative_to(VAULT).as_posix()
        rel_to_repo = page.relative_to(REPO_ROOT).as_posix()
        targets.add(pathlib.PurePosixPath(rel_to_vault).with_suffix("").as_posix())
        targets.add(rel_to_vault)
        if page.name == "index.md":
            parent_dir = pathlib.PurePosixPath(rel_to_vault).parent.as_posix()
            if parent_dir and parent_dir != ".":
                targets.add(parent_dir)
        targets.add(rel_to_repo)
    return targets


def _check_page_sources(
    rel: str,
    sources: object,
    transcript_paths: set[pathlib.Path],
    findings: list[Finding],
) -> None:
    if sources is None:
        return
    if not isinstance(sources, list):
        findings.append(
            Finding(
                "error",
                rel,
                "SOURCE_SCALAR",
                f"sources must be a list, got {type(sources).__name__}",
            )
        )
        return
    for source in sources:
        if not isinstance(source, str) or not source:
            findings.append(
                Finding(
                    "error",
                    rel,
                    "SOURCE_NON_STRING",
                    f"sources must be a list of non-empty strings; got {type(source).__name__}={source!r}",
                )
            )
            continue
        candidate = (
            (RAW / source[len("raw/") :]).resolve()
            if source.startswith("raw/")
            else (RAW / source).resolve()
        )
        if not _under_raw(candidate):
            findings.append(
                Finding(
                    "error",
                    rel,
                    "SOURCE_TRAVERSAL",
                    f"sources entry resolves outside kb/raw/: {source!r}",
                )
            )
        elif not candidate.is_file():
            findings.append(
                Finding("error", rel, "SOURCE_MISSING", f"sources entry not committed: {source!r}")
            )
        elif candidate not in transcript_paths:
            findings.append(
                Finding(
                    "error",
                    rel,
                    "SOURCE_NOT_MANIFESTED",
                    f"sources entry {source!r} has no manifest row",
                )
            )


def _check_page_links(
    page: pathlib.Path,
    targets: set[str],
    transcript_paths: set[pathlib.Path],
    fingerprints: set[str],
    findings: list[Finding],
) -> None:
    rel = page.relative_to(REPO_ROOT).as_posix()
    text = page.read_text(encoding="utf-8")
    fm, fm_findings = check_frontmatter(pathlib.Path(rel), text)
    findings.extend(fm_findings)
    if not fm and rel not in _BARE_FILE_PATHS:
        return
    for match in WIKILINK_RE.finditer(text):
        target = match.group(1).strip()
        if target not in targets:
            findings.append(Finding("error", rel, "WIKILINK", f"unresolved [[{target}]]"))
    _check_page_sources(rel, fm.get("sources"), transcript_paths, findings)
    if fm.get("type") != "question-review":
        return
    fingerprint = fm.get("question_fingerprint", "")
    if fingerprint and fingerprint in fingerprints:
        findings.append(
            Finding(
                "error", rel, "REVIEW_DUP", f"duplicate question_fingerprint {fingerprint[:8]}…"
            )
        )
    if fingerprint:
        fingerprints.add(fingerprint)


def _cited_transcripts(pages: list[pathlib.Path]) -> set[pathlib.Path]:
    cited: set[pathlib.Path] = set()
    for page in pages:
        rel = page.relative_to(REPO_ROOT)
        fm, _ = check_frontmatter(pathlib.Path(rel), page.read_text(encoding="utf-8"))
        sources = fm.get("sources") if fm else None
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, str) or not source:
                continue
            candidate = (
                (RAW / source[len("raw/") :]).resolve()
                if source.startswith("raw/")
                else (RAW / source).resolve()
            )
            if _under_raw(candidate):
                cited.add(candidate)
    return cited


def _warn_orphaned_transcripts(
    manifest_rows: list[dict], cited: set[pathlib.Path], findings: list[Finding]
) -> None:
    for transcript in sorted(manifest_rows, key=lambda row: row.get("transcript", "")):
        if not transcript.get("transcript"):
            continue
        path = (RAW / transcript["transcript"]).resolve()
        if not _under_raw(path) or path in cited:
            continue
        findings.append(
            Finding(
                "warning",
                "kb/raw/source-manifest.jsonl",
                "TRANSCRIPT_ORPHAN",
                f"transcript {transcript['transcript']!r} is not cited by any summary or concept",
            )
        )


def check_links_and_refs(
    pages: list[pathlib.Path],
    all_pages: list[pathlib.Path],
    manifest_rows: list[dict],
    findings: list[Finding],
) -> tuple[set[str], list[pathlib.Path]]:
    """Walk every .md under the vault. Verify:
      - all [[wikilinks]] resolve to a committed page
      - all `sources` entries reference committed transcripts
      - no duplicate question_fingerprint across question-review pages
      - no orphaned authored page (a page referenced from index/nav only)
    Returns (wikilink_targets, page_paths_rel).

    `pages` is the authored/retrieval-index set; `all_pages` is the
    full set (including templates and generated reports) used
    purely for wikilink target resolution. Frontmatter + sources
    checks are still only run on `pages`.
    """
    targets = _link_targets(all_pages)
    transcript_paths = {
        (RAW / r["transcript"]).resolve() for r in manifest_rows if r.get("transcript")
    }
    fingerprints: set[str] = set()
    for page in pages:
        _check_page_links(page, targets, transcript_paths, fingerprints, findings)
    _warn_orphaned_transcripts(manifest_rows, _cited_transcripts(pages), findings)
    return targets, all_pages


# ---------------------------------------------------------------------------
# Nav sanity (sections present)
# ---------------------------------------------------------------------------

REQUIRED_NAV_SECTIONS = ["Summaries", "Concepts"]


def check_nav_sections(findings: list[Finding]) -> list[str]:
    """Return the section names actually present in kb/wiki/index.md. A missing
    required section is an error so the build (which inherits the index) keeps
    discoverability. The 'Review Templates' and 'Reviews' sub-sections share a
    single 'Templates & reviews' heading in the current index; the check
    accepts either a single combined heading or two separate ones."""
    idx = VAULT / "index.md"
    if not idx.exists():
        findings.append(
            Finding("error", "kb/wiki/index.md", "INDEX_MISSING", "index.md is required")
        )
        return []
    sections: list[str] = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            sections.append(line[3:].strip())
    for req in REQUIRED_NAV_SECTIONS:
        if not any(req == s or s.startswith(req) for s in sections):
            findings.append(
                Finding(
                    "error", "kb/wiki/index.md", "NAV_SECTION", f"missing required section: {req}"
                )
            )
    # Review Templates + Reviews: either each present individually, or a
    # combined heading that mentions both. PR-43 review: previously the
    # 'combined' predicate reused the templates heuristic
    # ("review template" in s.lower()), so a heading like "## Review
    # Templates" would satisfy both requirements even when there was no
    # separate Reviews section at all. Require the combined heading to
    # denote both - e.g. "Review Templates & Reviews" - by looking for
    # the words "template" and "review" in distinct forms.
    has_rt = any("review template" in s.lower() for s in sections)
    has_rv = any(s.lower() == "reviews" for s in sections)
    # A truly combined heading must mention both words and not be just
    # the templates heading. Match "templates" AND either "review"
    # (template-and-review form) or the plural "reviews".
    combined = any(
        ("template" in s.lower())
        and ("review" in s.lower() or "reviews" in s.lower())
        and s.lower().strip() != "review templates"
        for s in sections
    )
    if not (combined or (has_rt and has_rv)):
        findings.append(
            Finding(
                "error",
                "kb/wiki/index.md",
                "NAV_SECTION",
                "missing Review Templates and Reviews sections "
                "(either as separate sections or a single "
                "'Templates & reviews' heading)",
            )
        )
    return sections


# ---------------------------------------------------------------------------
# Retrieval index
# ---------------------------------------------------------------------------


def build_retrieval_index(
    pages: list[pathlib.Path],
    manifest_rows: list[dict],
    sections: list[str],
) -> dict:
    """A deterministic JSON dump suitable for KB-aware explanations (#36).

    Sorted keys everywhere; no mtime/path-order noise; stable schema."""
    out_pages: list[dict] = []
    for p in pages:
        rel = p.relative_to(REPO_ROOT).as_posix()
        text = p.read_text(encoding="utf-8")
        fm, _ = check_frontmatter(pathlib.Path(rel), text)
        if not fm:
            continue
        # tags normalised to sorted list of strings
        tags = fm.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        wikilinks = sorted(set(WIKILINK_RE.findall(text)))
        out_pages.append(
            {
                "path": rel,
                "type": fm.get("type", ""),
                "title": fm.get("title", ""),
                "description": fm.get("description", ""),
                "tags": sorted(tags),
                "sources": sorted(fm.get("sources") or []),
                "wikilinks": wikilinks,
                "question_fingerprint": fm.get("question_fingerprint", ""),
            }
        )
    out_pages.sort(key=lambda d: (d["type"], d["path"]))
    return {
        "schema_version": 1,
        "vault": "kb/wiki",
        "sources": sorted(
            (
                {
                    "source_id": r.get("source_id", ""),
                    "title": r.get("title", ""),
                    "tags": [],  # source-level tags not modeled yet
                    "url": r.get("url", ""),
                    "transcript": r.get("transcript", ""),
                }
                for r in manifest_rows
            ),
            key=lambda d: d["source_id"],
        ),
        "pages": out_pages,
        "nav_sections": sorted(sections),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check", action="store_true", help="validate only; do not write the retrieval index"
    )
    ap.add_argument(
        "--index-out",
        type=pathlib.Path,
        default=DEFAULT_INDEX,
        help=f"path to write the retrieval index (default: {DEFAULT_INDEX.relative_to(REPO_ROOT)})",
    )
    ap.add_argument("--json", action="store_true", help="emit findings as JSON on stdout")
    ap.add_argument(
        "--quiet", action="store_true", help="suppress the human-readable summary on stderr"
    )
    return ap.parse_args()


def _scan_kb(findings: list[Finding]) -> tuple[list[dict], list[pathlib.Path], list[str]]:
    manifest_rows = check_manifest(findings)
    pages = discover_md(VAULT)
    all_pages = _all_markdown_for_link_resolution(VAULT)
    check_links_and_refs(pages, all_pages, manifest_rows, findings)
    return manifest_rows, pages, check_nav_sections(findings)


def _emit_findings(args: argparse.Namespace, findings: list[Finding]) -> None:
    if args.json:
        print(json.dumps([dataclasses.asdict(f) for f in findings], sort_keys=True, indent=2))
    elif not args.quiet:
        for finding in findings:
            print(finding.render(), file=sys.stderr)


def _write_index(
    args: argparse.Namespace,
    pages: list[pathlib.Path],
    manifest_rows: list[dict],
    sections: list[str],
    findings: list[Finding],
) -> None:
    index = build_retrieval_index(pages, manifest_rows, sections)
    text = json.dumps(index, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    args.index_out.parent.mkdir(parents=True, exist_ok=True)
    args.index_out.write_text(text, encoding="utf-8")
    _emit_findings(args, findings)
    if args.json or args.quiet:
        return
    try:
        rel_path = args.index_out.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = args.index_out.resolve()
    print(
        f"wrote {rel_path} ({len(index['pages'])} pages, {len(index['sources'])} sources)",
        file=sys.stderr,
    )


def _check_index(
    args: argparse.Namespace,
    pages: list[pathlib.Path],
    manifest_rows: list[dict],
    sections: list[str],
    findings: list[Finding],
) -> None:
    index = build_retrieval_index(pages, manifest_rows, sections)
    regenerated = json.dumps(index, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if not args.index_out.is_file():
        findings.append(
            Finding(
                "error",
                args.index_out.resolve().as_posix(),
                "INDEX_MISSING",
                "committed retrieval index is missing; run scripts/check_kb.py (without --check) "
                "to (re)generate it, then commit the result",
            )
        )
    elif args.index_out.read_text(encoding="utf-8") != regenerated:
        try:
            stale_path = args.index_out.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            stale_path = args.index_out.resolve().as_posix()
        findings.append(
            Finding(
                "error",
                stale_path,
                "INDEX_STALE",
                "committed retrieval index is stale relative to the vault; run scripts/check_kb.py "
                "(without --check) to regenerate, then commit the result",
            )
        )
    _emit_findings(args, findings)


def main() -> int:
    args = _parse_args()

    findings: list[Finding] = []
    manifest_rows, pages, sections = _scan_kb(findings)
    if args.check:
        _check_index(args, pages, manifest_rows, sections, findings)
    else:
        _write_index(args, pages, manifest_rows, sections, findings)

    errors = [finding for finding in findings if finding.level == "error"]
    warnings = [finding for finding in findings if finding.level == "warning"]
    if not args.json and not args.quiet:
        print(
            f"KB lint: {len(errors)} error(s), {len(warnings)} warning(s), "
            f"{len(pages)} page(s) scanned",
            file=sys.stderr,
        )

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
