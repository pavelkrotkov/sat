#!/usr/bin/env bash
#
# Install the systemd units on the box that serves. Idempotent: re-run after
# changing HOST/PORT or after pulling a new revision of the units.
#
# Usage:  sudo -E deploy/install.sh
# Env:    SATPREP_BIND (default 0.0.0.0)   SATPREP_PORT (default 8765)
#         SATPREP_SERVICE_USER (default the invoking user)

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIND="${SATPREP_BIND:-0.0.0.0}"
PORT="${SATPREP_PORT:-8765}"
RUN_AS="${SATPREP_SERVICE_USER:-${SUDO_USER:-$USER}}"

die() { printf 'install: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "needs root: sudo -E deploy/install.sh"
id "$RUN_AS" >/dev/null 2>&1 || die "no such user: $RUN_AS"

# systemd runs with a minimal PATH, so the unit needs uv's absolute path.
UV="$(sudo -u "$RUN_AS" sh -lc 'command -v uv' || true)"
[ -n "$UV" ] || die "uv not found for user $RUN_AS"

# A replacement value is data, not sed syntax. A path containing | (the
# delimiter), & (the whole match) or a backslash would otherwise corrupt the
# rendered unit rather than fail loudly.
esc() { printf '%s' "$1" | sed -e 's/[\|&]/\\&/g'; }

render() {
    sed -e "s|__USER__|$(esc "$RUN_AS")|g" -e "s|__DIR__|$(esc "$REPO")|g" \
        -e "s|__UV__|$(esc "$UV")|g" -e "s|__HOST__|$(esc "$BIND")|g" \
        -e "s|__PORT__|$(esc "$PORT")|g" \
        "$REPO/deploy/$1" > "/etc/systemd/system/$1"
}

for unit in satprep.service satprep-backup.service satprep-backup.timer; do
    render "$unit"
    echo "  /etc/systemd/system/$unit"
done

# Not -g "$RUN_AS": a service account's primary group often has a different
# name (nobody/nogroup), and install would read the username as a group.
GROUP="$(id -gn "$RUN_AS")"
install -d -o "$RUN_AS" -g "$GROUP" "$REPO/data" "$REPO/exports" "$REPO/backups"

# install -d touches directories only. A database seeded by the login user
# before this runs stays owned by them, and the service account then opens it
# read-only - which surfaces as a SQLite write error mid-drill, not at start.
chown -R "$RUN_AS:$GROUP" "$REPO/data" "$REPO/exports" "$REPO/backups"

# The unit runs uv with --no-sync, because neither the project environment nor
# uv's cache is writable once the sandbox applies. Build it here instead.
sudo -u "$RUN_AS" sh -lc "cd '$REPO' && uv sync --frozen"

systemctl daemon-reload
systemctl enable --now satprep.service satprep-backup.timer

# hermes.local, so nobody has to remember a DHCP lease.
#
# `enable --now` runs unconditionally, because enablement and activity are
# independent states: guarding on either one alone leaves the other broken.
# is-enabled skips a stopped daemon, is-active skips a running-but-disabled
# one that then vanishes at the next reboot. The command is idempotent, so
# there is nothing to guard.
if ! command -v avahi-daemon >/dev/null; then
    echo "  installing avahi-daemon for mDNS"
    apt-get install -y avahi-daemon >/dev/null
fi
systemctl enable --now avahi-daemon

echo
systemctl --no-pager --lines=0 status satprep.service || true
echo
echo "==> http://$(hostname).local:$PORT"
