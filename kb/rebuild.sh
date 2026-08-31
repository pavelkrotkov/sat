#!/usr/bin/env bash
# Rebuild the SAT Prep knowledge base MkDocs site.
#
# Path-independent wrapper around the shared research-fabric wiki builder:
#   research-fabric/tools/wiki/build_wiki.py
# It derives the repository and vault paths from this script's own location, so
# it works from any checkout (no hard-coded /home/pavel/dev/sat paths). Markdown
# input and the rendered site are written to the gitignored kb/build/ directory.
#
# Prerequisites (see kb/README.md):
#   - the shared builder script (research-fabric/tools/wiki/build_wiki.py), and
#   - a `mkdocs` binary (with the `material` theme) in PATH.
# Override locations with SAT_WIKI_BUILDER and SAT_WIKI_MKDOCS respectively.
#
# Usage:
#   ./kb/rebuild.sh
#   SAT_WIKI_BUILDER=/path/to/build_wiki.py SAT_WIKI_MKDOCS=/path/to/mkdocs ./kb/rebuild.sh
#
# The built site is a disposable presentation layer; kb/wiki/ is the source of
# truth. See kb/README.md for deploy and verification commands.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="$REPO/kb/wiki"
BUILD="$REPO/kb/build"                        # gitignored: staging vault + site
STAGE="$BUILD/stage"                          # clean vault copy (no runtime junk)
SITE_SRC="$BUILD/site-src"

BUILDER="${SAT_WIKI_BUILDER:-/home/pavel/research-fabric/tools/wiki/build_wiki.py}"
MKDOCS="${SAT_WIKI_MKDOCS:-mkdocs}"

SITE_NAME="${SAT_WIKI_SITE_NAME:-SAT Prep KB}"
SITE_DESC="${SAT_WIKI_SITE_DESC:-Evidence-backed SAT Reading and Writing strategy knowledge base}"

die() { echo "error: $*" >&2; exit 1; }

# --- fail clearly and early when a prerequisite is missing -------------------
[[ -f "$BUILDER" ]] || die "shared builder not found at '$BUILDER' (set SAT_WIKI_BUILDER)"
command -v "$MKDOCS" >/dev/null 2>&1 || die "mkdocs not found on PATH (set SAT_WIKI_MKDOCS)"
[[ -d "$VAULT" ]] || die "vault not found at '$VAULT'"

# --- stage a clean vault copy ------------------------------------------------
# Copy committed-authored pages only. Local generated lint reports and OpenKB
# runtime state (kb/wiki/reports/, kb/.openkb/) must never reach the site.
rm -rf "$STAGE"
mkdir -p "$STAGE"
# copy all of vault, then drop anything that is not authored content
cp -R "$VAULT"/. "$STAGE"/
find "$STAGE" -type d \( -name reports -o -name '.openkb' \) -prune -exec rm -rf {} +
find "$STAGE" -type f -name '*.md' -path '*/reports/*' -delete

# --- build Markdown input + mkdocs.yml ---------------------------------------
python3 "$BUILDER" \
  --vault "$STAGE" \
  --site "$SITE_NAME" \
  --desc "$SITE_DESC" \
  --docs "$SITE_SRC/docs"

# --- add Templates/Reviews to the site nav -----------------------------------
# The shared builder only emits nav for Home/Summaries/Concepts/Entities/Sources.
# Templates and reviews are project-specific authored content, so inject them
# here so they render in the navigation (and are not left as orphans).
python3 - "$SITE_SRC/mkdocs.yml" <<'PY'
import sys, yaml, pathlib

cfg_path = pathlib.Path(sys.argv[1])
cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
nav = cfg.get("nav", [])
existing = {next(iter(s)) for s in nav if isinstance(s, dict)}
proj = cfg_path.parent            # mkdocs project root (site-src)
docs = proj / "docs"              # markdown pages live under the docs dir

def entry(path):
    stem = path.stem
    name = stem.replace("-", " ").title()
    if stem.lower() == "readme":
        name = "How to Author"
    return {name: path.relative_to(docs).as_posix()}

for title, sub in (("Review Templates", "review-templates"), ("Reviews", "reviews")):
    if title in existing:
        continue
    pages = sorted((docs / sub).rglob("*.md"))
    if pages:
        nav.append({title: [entry(p) for p in pages]})

cfg["nav"] = nav
cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
PY

# --- render the static site --------------------------------------------------
cd "$SITE_SRC"
"$MKDOCS" build --quiet
echo "rebuilt: $SITE_SRC/site"
echo "from vault: $VAULT"