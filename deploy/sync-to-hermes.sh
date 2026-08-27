#!/usr/bin/env bash
#
# Push newly scraped material from the ingest machine to the box that serves.
#
# One direction only. hermes owns data/satprep.db; this script never reads it
# and never sends one. What travels is raw source material, and ingest runs on
# the far side so that historical attempts land in the live database rather
# than in a copy that would then have to be merged back.
#
# Run from the repo root on the machine that has the Bluebook session.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${SATPREP_HOST:-hermes.local}"
USER="${SATPREP_USER:-$USER}"
REMOTE="${SATPREP_REMOTE_DIR:-/opt/satprep}"
TARGET="$USER@$HOST:$REMOTE"

die() { printf 'sync: %s\n' "$1" >&2; exit 1; }

[ -f "$REPO/outputs/wrong_questions.json" ] \
    || die "no outputs/wrong_questions.json; run the scraper first"

echo "==> $TARGET"

# The scrape result plus the HTML snapshots and figures it references. Ingest
# parses the snapshots, so sending the JSON alone would drop question bodies.
rsync -az --info=stats1 \
    "$REPO/outputs/wrong_questions.json" "$TARGET/outputs/"
rsync -az --delete --info=stats1 \
    "$REPO/artifacts/html/" "$TARGET/artifacts/html/"
rsync -az --info=stats1 \
    "$REPO/artifacts/images/" "$TARGET/artifacts/images/"

# A login shell, because uv installs to ~/.local/bin and a non-interactive ssh
# command gets the system PATH only - Debian's default .bashrc returns early
# before the line that would add it.
#
# No restart follows. satprep/server.py takes one connection per request via
# the get_conn dependency, so the next page load already sees the new corpus.
# Ingest is idempotent, so re-sending unchanged material is a no-op, and
# attempts already in the database are untouched either way.
ssh "$USER@$HOST" bash -lc "'cd \"$REMOTE\" && uv run --frozen satprep ingest && uv run --frozen satprep analyze'"

echo "==> http://$HOST:8765"
