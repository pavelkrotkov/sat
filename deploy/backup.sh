#!/usr/bin/env bash
#
# Nightly backup of the one file that cannot be rebuilt.
#
# Question content is recoverable from raw sources or from the JSONL archive.
# Attempt history, spacing state and the weakness cache exist only in
# data/satprep.db, and `satprep restore` deliberately does not carry them.
# Once drills run here, this box holds the only copy.
#
# Run from the repo root, or via deploy/satprep-backup.service.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${SATPREP_DB:-$REPO/data/satprep.db}"
DEST="${SATPREP_BACKUP_DIR:-$REPO/backups}"
KEEP="${SATPREP_BACKUP_KEEP:-30}"

die() { printf 'backup: %s\n' "$1" >&2; exit 1; }

[ -f "$DB" ] || die "no database at $DB"
command -v sqlite3 >/dev/null || die "sqlite3 not installed (apt install sqlite3)"

# Refuse to spend a retention slot on a database with no attempts in it: that
# is the shape a fresh or half-restored copy has, and 30 nights of it would
# roll every real backup off the end.
attempts=$(sqlite3 "$DB" "SELECT COUNT(*) FROM attempts;" 2>/dev/null || echo 0)
[ "$attempts" -gt 0 ] || die "database has no attempts; refusing to rotate backups"

mkdir -p "$DEST"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/satprep-$stamp.db"

# .backup, not cp: the server is running and the WAL is live, so copying the
# file alone can capture a torn page. This takes a consistent snapshot.
sqlite3 "$DB" ".backup '$out.tmp'"
sqlite3 "$out.tmp" "PRAGMA integrity_check;" | grep -qx ok \
    || { rm -f "$out.tmp"; die "integrity check failed; backup discarded"; }
mv "$out.tmp" "$out"
gzip -f "$out"

# The corpus archive rides along so a restore has content and state together.
if command -v uv >/dev/null; then
    uv run --frozen satprep export >/dev/null 2>&1 \
        && cp "$REPO/exports/corpus-v1.jsonl" "$DEST/corpus-$stamp.jsonl" \
        && gzip -f "$DEST/corpus-$stamp.jsonl"
fi

# Prune oldest first, counting only what this script writes.
ls -1t "$DEST"/satprep-*.db.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f
ls -1t "$DEST"/corpus-*.jsonl.gz 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm -f

printf 'backup: %s.gz (%s attempts)\n' "$out" "$attempts"
