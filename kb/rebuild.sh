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
#   SAT_WIKI_DEPLOY=/path/to/site/root ./kb/rebuild.sh   # also publishes
#
# The built site is a disposable presentation layer; kb/wiki/ is the source of
# truth. See kb/README.md for deploy and verification commands.
set -euo pipefail

# SAT_KB_ROOT re-roots the script for tests and unusual layouts (e.g. a
# hermetic CI run that copies kb/ to a tempdir). The script's default
# behaviour is to derive REPO from its own location.
if [[ -n "${SAT_KB_ROOT:-}" ]]; then
    REPO="$(cd "$SAT_KB_ROOT" && pwd)"
else
    REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
VAULT="$REPO/kb/wiki"
BUILD="$REPO/kb/build"                        # gitignored: staging vault + site
STAGE="$BUILD/stage"                          # clean vault copy (no runtime junk)
SITE_SRC="$BUILD/site-src"

BUILDER="${SAT_WIKI_BUILDER:-/home/pavel/research-fabric/tools/wiki/build_wiki.py}"
MKDOCS="${SAT_WIKI_MKDOCS:-mkdocs}"
DEPLOY="${SAT_WIKI_DEPLOY:-}"                 # optional publish target
SITE_NAME="${SAT_WIKI_SITE_NAME:-SAT Prep KB}"
SITE_DESC="${SAT_WIKI_SITE_DESC:-Evidence-backed SAT Reading and Writing strategy knowledge base}"

die() { echo "error: $*" >&2; exit 1; }

# --- fail clearly and early when a prerequisite is missing -------------------
[[ -f "$BUILDER" ]] || die "shared builder not found at '$BUILDER' (set SAT_WIKI_BUILDER)"
command -v "$MKDOCS" >/dev/null 2>&1 || die "mkdocs not found on PATH (set SAT_WIKI_MKDOCS)"
[[ -d "$VAULT" ]] || die "vault not found at '$VAULT'"

# Reject any symlink in the vault BEFORE running the lint or staging. A
# symlink whose target is outside the vault (e.g. /home/pavel/.ssh/id_rsa)
# would otherwise be expanded by `cp -R` into the staged site and published.
# This is a hard pre-flight gate; even a "harmless" symlink to a real file
# inside the vault is rejected because the vault is content, not a graph
# of links.
while IFS= read -r -d '' link; do
    die "refusing to stage vault: symlink at ${link#"$VAULT"/}"
done < <(find "$VAULT" -type l -print0)

# --- validate the KB before building ---------------------------------------
# scripts/check_kb.py is the deterministic validator from issue #39. It
# checks required frontmatter, manifest provenance, wikilinks, transcript
# citations, nav sections, and the retrieval index. A failure here blocks
# the build so a broken KB cannot reach the rendered site. The index is
# regenerated as a side effect (see kb/README.md for the version policy).
LINT="$REPO/scripts/check_kb.py"
[[ -f "$LINT" ]] || die "KB lint script not found at '$LINT' (expected scripts/check_kb.py)"
if ! python3 "$LINT"; then
    die "KB lint failed; fix the findings above and re-run"
fi

# Constrain STAGE so a misconfigured $BUILD cannot redirect rm -rf elsewhere.
case "$STAGE" in
    "$BUILD"/*) ;;
    *) die "refusing to use STAGE='$STAGE' (not under BUILD='$BUILD')" ;;
esac

# --- stage a clean vault copy ------------------------------------------------
# Copy committed-authored pages only. -RL prevents following symlinks out of
# the vault (no symlinks should be committed; this is defense-in-depth).
# Local generated lint reports and OpenKB runtime state (kb/wiki/reports/,
# kb/.openkb/) must never reach the site.
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$VAULT"/. "$STAGE"/
# Drop excluded subtrees so they don't reach the site even if a future commit
# slips one in. reports/ contains generated lint reports; .openkb/ is runtime.
find "$STAGE" -type d \( -name reports -o -name '.openkb' \) -prune -exec rm -rf {} +

# --- build Markdown input + mkdocs.yml ---------------------------------------
python3 "$BUILDER" \
  --vault "$STAGE" \
  --site "$SITE_NAME" \
  --desc "$SITE_DESC" \
  --docs "$SITE_SRC/docs"

# --- add Review-Templates/Reviews to the site nav ----------------------------
# The shared builder only emits nav for Home/Summaries/Concepts/Entities/Sources.
# Templates and reviews are project-specific authored content, so inject them
# here so they render in the navigation (and are not left as orphans). We use
# MkDocs' own interpreter for the YAML edit so PyYAML availability follows
# SAT_WIKI_MKDOCS, not the unrelated system python3.
"$MKDOCS" --help >/dev/null  # warm; ensures the binary we just checked is still the one we run
MKDOCS_PY="$(command -v "$MKDOCS")"
"$(dirname "$MKDOCS_PY")/python3" - "$SITE_SRC/mkdocs.yml" <<'PY'
import sys, yaml, pathlib

cfg_path = pathlib.Path(sys.argv[1])
cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
nav = cfg.get("nav", [])
existing = {next(iter(s)) for s in nav if isinstance(s, dict) and s}
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

# --- optional publish step ---------------------------------------------------
# SAT_WIKI_DEPLOY is the service document root (e.g. /home/pavel/services/sat-wiki/site-src/site).
# When unset we only build; the caller decides whether to publish.
if [[ -n "$DEPLOY" ]]; then
    mkdir -p "$DEPLOY"
    rsync -a --delete "$SITE_SRC/site/" "$DEPLOY/"
    echo "published: $DEPLOY"
fi