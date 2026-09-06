# Serving satprep on a home-network box

Runbook for an always-on Linux machine on a trusted local network. The serving
box owns `data/satprep.db`; do not maintain a second writable copy because
attempt/spacing histories are not mergeable.

## First-time setup

```sh
sudo apt install -y git sqlite3 avahi-daemon
curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/dev && git clone https://github.com/pavelkrotkov/sat ~/dev/sat
cd ~/dev/sat && uv sync --frozen
```

Use the serving box's actual hostname (Avahi normally advertises
`<hostname>.local`) or another resolvable DNS name/IP:

```sh
export SATPREP_HOST=server-hostname.local
ssh "$SATPREP_HOST" 'mkdir -p ~/dev/sat/{data,outputs,artifacts}'
rsync -avz data/satprep.db              "$SATPREP_HOST:dev/sat/data/"
rsync -avz outputs/wrong_questions.json "$SATPREP_HOST:dev/sat/outputs/"
rsync -avz artifacts/                   "$SATPREP_HOST:dev/sat/artifacts/"
```

The remote paths are intentionally relative to the remote home directory; a
local `$HOME` must not be expanded into an rsync/ssh target.

Install the units:

```sh
sudo -E deploy/install.sh
```

`SATPREP_BIND`, `SATPREP_PORT`, and `SATPREP_SERVICE_USER` override the service
defaults. Avahi provides the machine's normal `<hostname>.local` mDNS name.

## Keeping the corpus current

After a local scrape, set the serving host and sync:

```sh
SATPREP_HOST=server-hostname.local deploy/sync-to-server.sh
```

The script sends only `outputs/wrong_questions.json` plus the HTML/images that
ingest needs. It never sends `data/satprep.db`; ingest and analysis run on the
serving box. `SATPREP_HOST` is required rather than guessed. `SATPREP_USER` and
`SATPREP_REMOTE_DIR` are optional overrides (remote directory default:
`dev/sat`).

## Backups

`satprep-backup.timer` runs `deploy/backup.sh` nightly and retains 30 days of
gzipped database snapshots in the gitignored `backups/` directory. The script
uses SQLite `.backup` and `integrity_check` rather than copying a live WAL file.

Keep an off-box recovery copy, including figures referenced by the corpus:

```sh
rsync -avz "$SATPREP_HOST:dev/sat/backups/"          ~/satprep-backups/
rsync -avz "$SATPREP_HOST:dev/sat/artifacts/images/" ~/satprep-backups/images/
```

Restore with the service stopped and remove newer WAL sidecars before opening
the snapshot:

```sh
sudo systemctl stop satprep
rm -f data/satprep.db-wal data/satprep.db-shm
gunzip -c backups/satprep-<stamp>.db.gz > data/satprep.db
sudo systemctl start satprep
```

## Network safety

The web UI has no authentication. `satprep serve` therefore binds loopback by
default. Do not expose it directly to the internet; use a trusted private
network or an authenticated overlay/reverse proxy if remote access is needed.

## Checks

```sh
systemctl status satprep
journalctl -u satprep -f
systemctl list-timers satprep-backup
ls -lh backups/
sqlite3 data/satprep.db "SELECT COUNT(*) FROM attempts;"
```
