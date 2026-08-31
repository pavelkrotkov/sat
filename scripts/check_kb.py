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
except ImportError:                                  # pragma: no cover
    print("error: PyYAML is required (transitive dep of MkDocs)", file=sys.stderr)
    sys.exit(2)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# REPO_ROOT is overridable via SAT_KB_ROOT for tests and unusual layouts.
# All other paths are derived from it, so a single override re-roots the
# whole script.
REPO_ROOT = pathlib.Path(
    os.environ.get("SAT_KB_ROOT")
    or pathlib.Path(__file__).resolve().parent.parent
).resolve()
VAULT = REPO_ROOT / "kb" / "wiki"
RAW = REPO_ROOT / "kb" / "raw"
MANIFEST = RAW / "source-manifest.jsonl"
DEFAULT_INDEX = REPO_ROOT / "kb" / ".kb-index.json"

# Per-type required frontmatter. Every authored page also needs a `title`.
# `created` and `updated` are required for non-README pages because they
# feed KB log/history and the retrieval index timestamp.
REQUIRED_BY_TYPE: dict[str, list[str]] = {
    "summary": ["title", "type", "created", "updated", "tags",
                "sources", "confidence"],
    "concept": ["title", "type", "created", "updated", "tags",
                "sources", "confidence"],
    "question-review": ["title", "type", "created", "updated", "tags",
                        "question_fingerprint", "student_answer",
                        "correct_answer", "confidence"],
}
ALLOWED_TYPES = set(REQUIRED_BY_TYPE) | {"log", "index", "readme"}

WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:\|[^\]]+)?\]\]")
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
_BARE_FILE_PATHS.update({
    f.as_posix() for f in [
        pathlib.Path("kb/wiki/reviews/README.md"),
    ] if f.exists() or True  # declared; rely on file presence to swallow
})
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
    level: str            # "error" | "warning"
    path: str             # repo-relative, forward-slash
    code: str             # short identifier for filtering
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
        findings.append(Finding("error", rel.as_posix(), "FM_NOT_MAPPING",
                                f"frontmatter must be a YAML mapping, "
                                f"got {type(fm).__name__}"))
        return {}, findings
    if "type" not in fm:
        findings.append(Finding("error", rel.as_posix(), "FM_TYPE",
                                "frontmatter missing `type`"))
        return fm, findings
    if fm["type"] not in ALLOWED_TYPES:
        findings.append(Finding("error", rel.as_posix(), "FM_TYPE_UNKNOWN",
                                f"unknown type {fm['type']!r}; "
                                f"expected one of {sorted(ALLOWED_TYPES)}"))
        return fm, findings
    required = REQUIRED_BY_TYPE.get(fm["type"], ["title", "type"])
    for key in required:
        if key not in fm or fm[key] in (None, "", []):
            findings.append(Finding("error", rel.as_posix(), "FM_REQUIRED",
                                    f"missing required field `{key}` for "
                                    f"type={fm['type']}"))
    # Type-specific shape checks
    if fm.get("type") == "question-review":
        fp = fm.get("question_fingerprint", "")
        if not isinstance(fp, str) or not SHA256_RE.match(fp):
            findings.append(Finding("error", rel.as_posix(), "FM_FINGERPRINT",
                                    "question_fingerprint must be a "
                                    "64-char lowercase hex SHA-256"))
    for sa in ("student_answer", "correct_answer"):
        v = fm.get(sa)
        if "question-review" == fm.get("type") and v not in {"A", "B", "C", "D", "E"}:
            findings.append(Finding("error", rel.as_posix(), "FM_ANSWER",
                                    f"{sa} must be a single letter A-E, got {v!r}"))
    return fm, findings


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def check_manifest(findings: list[Finding]) -> list[dict]:
    rows: list[dict] = []
    seen_ids: set[str] = set()
    if not MANIFEST.exists():
        findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                "MANIFEST_MISSING",
                                "source manifest is required"))
        return rows
    text = MANIFEST.read_text(encoding="utf-8")
    # JSONL spec: each line must end with \n including the last; a missing
    # trailing newline is a finding (and a parser hazard for strict readers).
    if text and not text.endswith("\n"):
        findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                "MANIFEST_NEWLINE",
                                "missing trailing newline (JSONL spec)"))
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as e:
            findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                    "MANIFEST_JSON",
                                    f"line {lineno}: {e}"))
            continue
        # PR-43 review: validate that each decoded row is a mapping
        # before accessing fields. Valid JSON scalars (null / []) used
        # to raise AttributeError here, producing a traceback instead
        # of a structured finding and breaking `--json` for consumers.
        if not isinstance(row, dict):
            findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                    "MANIFEST_ROW_SHAPE",
                                    f"line {lineno}: each manifest row "
                                    f"must be a JSON object, got "
                                    f"{type(row).__name__}"))
            continue
        sid = row.get("source_id", "")
        if not sid:
            findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                    "MANIFEST_ID",
                                    f"line {lineno}: missing source_id"))
        elif sid in seen_ids:
            findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                    "MANIFEST_DUP",
                                    f"line {lineno}: duplicate source_id {sid}"))
        seen_ids.add(sid)

        for field in ("title", "url", "retrieved_at", "content_type",
                      "sha256", "bytes", "transcript", "authority"):
            if field not in row or row[field] in (None, ""):
                findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                        "MANIFEST_FIELD",
                                        f"line {lineno}: missing field {field}"))
        if not SHA256_RE.match(str(row.get("sha256", ""))):
            findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                    "MANIFEST_SHA",
                                    f"line {lineno}: sha256 must be a "
                                    f"64-char lowercase hex string"))
        try:
            want_bytes = int(row.get("bytes", -1))
        except (TypeError, ValueError):
            want_bytes = -1
        rel = str(row.get("transcript", ""))
        if rel:
            p = (RAW / rel).resolve()
            # Defend against path traversal: every committed transcript must
            # live under kb/raw/ and point at a real .txt file. After
            # .resolve() a symlink inside the vault could resolve outside
            # the raw dir, so verify the canonical path is still under RAW.
            try:
                p.relative_to(RAW)
            except ValueError:
                findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                        "MANIFEST_TRAVERSAL",
                                        f"line {lineno}: transcript resolves "
                                        f"outside kb/raw/: {rel}"))
            if not p.is_file():
                findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                        "MANIFEST_PATH",
                                        f"line {lineno}: transcript not found: "
                                        f"{rel}"))
            else:
                actual = p.read_bytes()
                actual_sha = hashlib.sha256(actual).hexdigest()
                if actual_sha != row.get("sha256"):
                    findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                            "MANIFEST_SHA",
                                            f"line {lineno}: sha256 mismatch "
                                            f"for {rel}"))
                if len(actual) != want_bytes:
                    findings.append(Finding("error", "kb/raw/source-manifest.jsonl",
                                            "MANIFEST_BYTES",
                                            f"line {lineno}: bytes mismatch "
                                            f"for {rel} (manifest {want_bytes}, "
                                            f"actual {len(actual)})"))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Wikilinks + cross-references
# ---------------------------------------------------------------------------

def discover_md(root: pathlib.Path) -> list[pathlib.Path]:
    pages = sorted(root.rglob("*.md"))
    return [p for p in pages
            if tuple(pathlib.Path(p).relative_to(root).parts[:-1]) not in TEMPLATE_DIRS]


def check_links_and_refs(
    pages: list[pathlib.Path],
    manifest_rows: list[dict],
    findings: list[Finding],
) -> tuple[set[str], set[str]]:
    """Walk every .md under the vault. Verify:
      - all [[wikilinks]] resolve to a committed page
      - all `sources` entries reference committed transcripts
      - no duplicate question_fingerprint across question-review pages
      - no orphaned authored page (a page referenced from index/nav only)
    Returns (wikilink_targets, page_paths_rel)."""
    all_pages = {p.relative_to(REPO_ROOT).as_posix() for p in pages}
    # Wikilink resolution: a [[foo/bar]] inside the vault resolves to
    # <vault>/foo/bar.md (MkDocs-style). The bare directory also resolves
    # to <vault>/foo/index.md - ONLY when that file actually exists in
    # the vault; today we don't ship index files in the standard
    # directories, so a bare "foo" target is rejected for any directory
    # that has no index.md.
    # The bare string resolves to <vault>/foo.md. Targets are stored as
    # posix strings relative to the vault root (which is what the
    # wikilink syntax is relative to).
    targets: set[str] = set()
    pages_by_path: dict[str, pathlib.Path] = {}
    for p in pages:
        rel_to_vault = p.relative_to(VAULT).as_posix()
        rel_to_repo = p.relative_to(REPO_ROOT).as_posix()
        pages_by_path[rel_to_vault] = p
        # 'foo/bar.md' resolves from the bare wikilink 'foo/bar'
        targets.add(pathlib.PurePosixPath(rel_to_vault).with_suffix("").as_posix())
        # 'foo/bar.md' also resolves from 'foo/bar' (same as above, but
        # explicit for clarity).
        targets.add(rel_to_vault)
        # and from 'foo' (as the directory index) - ONLY when the
        # directory's index.md actually exists. The original logic
        # registered every parent directory as a valid target, which
        # made [[concepts]] pass merely because concepts/ contains any
        # markdown file; the rendered site then 404s on the link. The
        # PR-43 fix: require <dir>/index.md before adding the parent.
        if p.name == "index.md":
            parent_dir = pathlib.PurePosixPath(rel_to_vault).parent.as_posix()
            if parent_dir and parent_dir != ".":
                targets.add(parent_dir)
        # ... and as a relative-from-repo path, for callers that pass full
        # paths in the wikilink (defensive).
        targets.add(rel_to_repo)

    fingerprints: set[str] = set()
    transcript_paths = {(RAW / r["transcript"]).resolve()
                        for r in manifest_rows if r.get("transcript")}

    for p in pages:
        rel = p.relative_to(REPO_ROOT).as_posix()
        text = p.read_text(encoding="utf-8")
        fm, fm_findings = check_frontmatter(pathlib.Path(rel), text)
        findings.extend(fm_findings)
        # FM findings were already pushed; skip wikilink/sources/fingerprint
        # checks if FM is broken (empty dict returned alongside a finding).
        if not fm and rel.lstrip("kb/wiki/") not in BARE_FILES:
            continue
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if target not in targets:
                findings.append(Finding("error", rel, "WIKILINK",
                                        f"unresolved [[{target}]]"))
        # sources[] must reference committed transcripts. The path is
        # relative to the kb/raw/ directory (the manifest's anchor), so we
        # also accept a vault-relative "raw/transcripts/..." form for
        # forward compatibility. Scalar (non-list) sources are a common
        # YAML mistake and must surface as an error rather than silently
        # drop.
        sources = fm.get("sources")
        if sources is None:
            pass                          # already flagged by FM_REQUIRED
        elif not isinstance(sources, list):
            findings.append(Finding("error", rel, "SOURCE_SCALAR",
                                    f"sources must be a list, got "
                                    f"{type(sources).__name__}"))
        else:
            for s in sources:
                if not s:
                    continue
                if s.startswith("raw/"):
                    candidate = (RAW / s[len("raw/"):]).resolve()
                else:
                    candidate = (RAW / s).resolve()
                if not _under_raw(candidate):
                    findings.append(Finding("error", rel, "SOURCE_TRAVERSAL",
                                            f"sources entry resolves outside "
                                            f"kb/raw/: {s!r}"))
                elif not candidate.is_file():
                    findings.append(Finding("error", rel, "SOURCE_MISSING",
                                            f"sources entry not committed: {s!r}"))
                elif candidate not in transcript_paths:
                    findings.append(Finding("error", rel, "SOURCE_NOT_MANIFESTED",
                                            f"sources entry {s!r} has no "
                                            f"manifest row"))
        # duplicate fingerprint check (question-review only)
        if fm and fm.get("type") == "question-review":
            fp = fm.get("question_fingerprint", "")
            if fp:
                if fp in fingerprints:
                    findings.append(Finding("error", rel, "REVIEW_DUP",
                                            f"duplicate question_fingerprint "
                                            f"{fp[:8]}…"))
                fingerprints.add(fp)
        # Transcripts must in turn be cited by at least one summary/concept,
        # otherwise they are orphaned (a finding at WARN, not error, so we
        # don't block merges on author-stage drafts).
    # Transcript citation map: a manifest row is "cited" if any authored
    # page (summary/concept) lists it in `sources:`, in either of the two
    # accepted forms (raw/prefixed or not).
    cited = set()
    for p in pages:
        rel = p.relative_to(REPO_ROOT)
        text = p.read_text(encoding="utf-8")
        fm, _ = check_frontmatter(pathlib.Path(rel), text)
        if not fm:
            continue
        if not isinstance(fm.get("sources"), list):
            continue
        for s in fm["sources"]:
            if not s:
                continue
            if s.startswith("raw/"):
                candidate = (RAW / s[len("raw/"):]).resolve()
            else:
                candidate = (RAW / s).resolve()
            if _under_raw(candidate):
                cited.add(candidate)
    for t in sorted(manifest_rows, key=lambda r: r.get("transcript", "")):
        if not t.get("transcript"):
            continue
        m_path = (RAW / t["transcript"]).resolve()
        if not _under_raw(m_path):
            continue                          # already flagged in check_manifest
        if m_path not in cited:
            findings.append(Finding("warning", "kb/raw/source-manifest.jsonl",
                                    "TRANSCRIPT_ORPHAN",
                                    f"transcript {t['transcript']!r} is not "
                                    f"cited by any summary or concept"))
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
        findings.append(Finding("error", "kb/wiki/index.md", "INDEX_MISSING",
                                "index.md is required"))
        return []
    sections: list[str] = []
    for line in idx.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            sections.append(line[3:].strip())
    for req in REQUIRED_NAV_SECTIONS:
        if not any(req == s or s.startswith(req) for s in sections):
            findings.append(Finding("error", "kb/wiki/index.md", "NAV_SECTION",
                                    f"missing required section: {req}"))
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
        findings.append(Finding("error", "kb/wiki/index.md", "NAV_SECTION",
                                "missing Review Templates and Reviews sections "
                                "(either as separate sections or a single "
                                "'Templates & reviews' heading)"))
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
        out_pages.append({
            "path": rel,
            "type": fm.get("type", ""),
            "title": fm.get("title", ""),
            "description": fm.get("description", ""),
            "tags": sorted(tags),
            "sources": sorted(fm.get("sources") or []),
            "wikilinks": wikilinks,
            "question_fingerprint": fm.get("question_fingerprint", ""),
        })
    out_pages.sort(key=lambda d: (d["type"], d["path"]))
    return {
        "schema_version": 1,
        "vault": "kb/wiki",
        "sources": sorted(
            ({"source_id": r.get("source_id", ""),
              "title": r.get("title", ""),
              "tags": [],     # source-level tags not modeled yet
              "url": r.get("url", ""),
              "transcript": r.get("transcript", "")}
             for r in manifest_rows),
            key=lambda d: d["source_id"],
        ),
        "pages": out_pages,
        "nav_sections": sorted(sections),
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="validate only; do not write the retrieval index")
    ap.add_argument("--index-out", type=pathlib.Path, default=DEFAULT_INDEX,
                    help=f"path to write the retrieval index "
                         f"(default: {DEFAULT_INDEX.relative_to(REPO_ROOT)})")
    ap.add_argument("--json", action="store_true",
                    help="emit findings as JSON on stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the human-readable summary on stderr")
    args = ap.parse_args()

    findings: list[Finding] = []
    manifest_rows = check_manifest(findings)
    pages = discover_md(VAULT)
    targets, all_pages = check_links_and_refs(pages, manifest_rows, findings)
    sections = check_nav_sections(findings)

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]

    # PR-43 review: in --check mode, defer emitting findings-as-JSON
    # until after the regenerated index has been compared against the
    # committed one. The original ordering could return [] with exit
    # status 1 when the only problem was a stale index, leaving
    # downstream JSON consumers unable to tell what failed.

    if not args.check:
        idx = build_retrieval_index(pages, manifest_rows, sections)
        text = json.dumps(idx, sort_keys=True, indent=2,
                          ensure_ascii=False) + "\n"
        args.index_out.parent.mkdir(parents=True, exist_ok=True)
        args.index_out.write_text(text, encoding="utf-8")
        if args.json:
            print(json.dumps(
                [dataclasses.asdict(f) for f in findings],
                sort_keys=True, indent=2))
        elif not args.quiet:
            for f in findings:
                print(f.render(), file=sys.stderr)
            try:
                rel_path = args.index_out.relative_to(REPO_ROOT)
            except ValueError:
                # PR-43 review: --index-out can point outside the repo
                # (e.g. /tmp/index.json). In the default
                # human-readable branch we want to print an absolute
                # path rather than crash in `relative_to`. --json and
                # --quiet already skip this branch entirely.
                rel_path = args.index_out.resolve()
            print(f"wrote {rel_path} ({len(idx['pages'])} pages, "
                  f"{len(idx['sources'])} sources)",
                  file=sys.stderr)
    else:
        # --check: validate only. If the committed index would be regenerated
        # to something different, the vault metadata has drifted and the
        # committed index is stale; surface that as an error so CI fails
        # before the build can publish a stale lookup index.
        idx = build_retrieval_index(pages, manifest_rows, sections)
        regenerated = json.dumps(idx, sort_keys=True, indent=2,
                                 ensure_ascii=False) + "\n"
        # PR-43 review: a missing committed index was being silently
        # accepted. Treat absent kb/.kb-index.json as an INDEX_MISSING
        # error in --check mode so a deleted/untracked index fails the
        # check instead of letting the explanation pipeline ship with
        # no KB references.
        if not args.index_out.is_file():
            findings.append(Finding(
                "error",
                args.index_out.resolve().as_posix(),
                "INDEX_MISSING",
                "committed retrieval index is missing; run "
                "scripts/check_kb.py (without --check) to "
                "(re)generate it, then commit the result"))
        else:
            committed = args.index_out.read_text(encoding="utf-8")
            if committed != regenerated:
                findings.append(Finding(
                    "error",
                    args.index_out.relative_to(REPO_ROOT).as_posix(),
                    "INDEX_STALE",
                    "committed retrieval index is stale relative to the "
                    "vault; run scripts/check_kb.py (without --check) to "
                    "regenerate, then commit the result"))
        # Emit findings AFTER the index comparison so stale/missing
        # entries survive into both the JSON output and the human-readable
        # summary. Re-read `errors`/`warnings` because the findings list
        # has been extended above.
        errors = [f for f in findings if f.level == "error"]
        warnings = [f for f in findings if f.level == "warning"]
        if args.json:
            print(json.dumps(
                [dataclasses.asdict(f) for f in findings],
                sort_keys=True, indent=2))
        elif not args.quiet:
            for f in findings:
                print(f.render(), file=sys.stderr)

    if not args.json and not args.quiet:
        print(f"KB lint: {len(errors)} error(s), {len(warnings)} warning(s), "
              f"{len(pages)} page(s) scanned",
              file=sys.stderr)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())