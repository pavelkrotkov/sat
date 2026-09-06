#!/usr/bin/env bash
# Push newly scraped material from the ingest machine to the serving box.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${SATPREP_HOST:-satprep.local}"
USER="${SATPREP_USER:-$USER}"
REMOTE="${SATPREP_REMOTE_DIR:-dev/sat}"
TARGET="$USER@$HOST:$REMOTE"

die() { printf 'sync: %s\n' "$1" >&2; exit 1; }

[ -f "$REPO/outputs/wrong_questions.json" ] \
    || die "no outputs/wrong_questions.json; run the scraper first"

echo "==> $TARGET"

rsync -az --info=stats1 \
    "$REPO/outputs/wrong_questions.json" "$TARGET/outputs/"
rsync -az --delete --info=stats1 \
    "$REPO/artifacts/html/" "$TARGET/artifacts/html/"
rsync -az --info=stats1 \
    "$REPO/artifacts/images/" "$TARGET/artifacts/images/"

ssh "$USER@$HOST" bash -lc "'cd \"$REMOTE\" && uv run --frozen satprep ingest && uv run --frozen satprep analyze'"

echo "==> http://$HOST:8765"
