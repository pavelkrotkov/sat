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

render() {
    sed -e "s|__USER__|$RUN_AS|g" -e "s|__DIR__|$REPO|g" -e "s|__UV__|$UV|g" \
        -e "s|__HOST__|$BIND|g" -e "s|__PORT__|$PORT|g" \
        "$REPO/deploy/$1" > "/etc/systemd/system/$1"
}

for unit in satprep.service satprep-backup.service satprep-backup.timer; do
    render "$unit"
    echo "  /etc/systemd/system/$unit"
done

install -d -o "$RUN_AS" -g "$RUN_AS" "$REPO/data" "$REPO/exports" "$REPO/backups"

systemctl daemon-reload
systemctl enable --now satprep.service satprep-backup.timer

# hermes.local, so nobody has to remember a DHCP lease.
if ! systemctl is-enabled --quiet avahi-daemon 2>/dev/null; then
    echo "  installing avahi-daemon for mDNS"
    apt-get install -y avahi-daemon >/dev/null
    systemctl enable --now avahi-daemon
fi

echo
systemctl --no-pager --lines=0 status satprep.service || true
echo
echo "==> http://$(hostname).local:$PORT"
