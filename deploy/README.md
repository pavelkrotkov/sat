# Serving satprep on a home-network box

Runbook for putting the web UI on a machine that stays on, so drills can be
taken from any device on the same wifi.

## The one rule

**The serving box owns `data/satprep.db`.**

Question content is rebuildable — from raw sources, or from the JSONL archive
alone. Attempt history, spacing state and the weakness cache are not: they
exist only in `data/satprep.db`, and `satprep restore` deliberately does not
carry them (see `satprep/corpus/archive.py`).

So once drills run here, this is the only copy. Two machines both accumulating
attempts cannot be merged afterwards — there is no code path that does it, and
adding one would mean reconciling two divergent spacing schedules. Keep the
ingest machine's copy as history and never write to it again.

| | ingest machine (laptop) | serving box (hermes) |
|---|---|---|
| Bluebook scraper, Playwright session | yes | no |
| `data/satprep.db` | frozen after the one-time seed | **source of truth** |
| `satprep ingest` | no — run it on the far side | yes |
| Backups | — | nightly, `deploy/backup.sh` |

## First-time setup

On the serving box:

```sh
sudo apt install -y git sqlite3 avahi-daemon
curl -LsSf https://astral.sh/uv/install.sh | sh

mkdir -p ~/dev && git clone https://github.com/pavelkrotkov/sat ~/dev/sat
cd ~/dev/sat && uv sync --frozen
```

Seed the database — **once**, before any drill is taken here:

```sh
# from the ingest machine. Each item goes to its own subdirectory: with
# several sources and one destination, rsync would flatten them all into
# ~/dev/sat, where nothing looks for them. The remote paths are relative:
# rsync and ssh resolve them against the remote home, whereas $HOME would
# expand here and send this machine's path to the other one.
ssh hermes.local 'mkdir -p ~/dev/sat/{data,outputs,artifacts}'
rsync -avz data/satprep.db            hermes.local:dev/sat/data/
rsync -avz outputs/wrong_questions.json hermes.local:dev/sat/outputs/
rsync -avz artifacts/                 hermes.local:dev/sat/artifacts/
```

`artifacts/images/` must exist before the service starts: `satprep/server.py`
mounts it at import time, and `StaticFiles` raises on a missing directory.

Then install the units:

```sh
sudo -E deploy/install.sh
```

This renders `satprep.service` and the nightly backup timer, enables both,
and makes sure `avahi-daemon` is running so the box answers to
`hermes.local`. Override the bind address or port with `SATPREP_BIND` and
`SATPREP_PORT`.

The UI is then at **http://hermes.local:8765** from any device on the network
— phone, iPad, laptop. The templates already carry a viewport meta, so the
mobile layout works.

## Keeping the corpus current

After a scrape on the ingest machine:

```sh
deploy/sync-to-hermes.sh
```

It sends `outputs/wrong_questions.json` plus the HTML snapshots and figures
that ingest parses, then runs `satprep ingest` and `satprep analyze` **on the
far side**. That matters: ingesting Bluebook wrong-answers creates historical
*attempts*, so it has to happen in the live database rather than in a copy
that would need merging back.

Set `SATPREP_HOST`, `SATPREP_USER` or `SATPREP_REMOTE_DIR` if your names
differ from `hermes.local`, your login, and `~/dev/sat`.

Fetching from the College Board question bank is a plain network call with no
browser session behind it, so run that directly on the serving box:

```sh
uv run --frozen satprep fetch-qbank --hard-only
```

## Backups

`satprep-backup.timer` runs `deploy/backup.sh` nightly, keeping 30 days of
gzipped snapshots in `backups/` alongside the corpus archive.

It uses `sqlite3 .backup` rather than `cp`, because the server is running and
the WAL is live — copying the file directly can capture a torn page. Each
snapshot is integrity-checked before it replaces the temp file, and the script
refuses to rotate if the database reports zero attempts, so a fresh or
half-restored copy cannot roll thirty good nights off the end.

Pull them somewhere else periodically — a backup that lives only on the box
it protects is not one:

```sh
rsync -avz hermes.local:dev/sat/backups/        ~/satprep-backups/
rsync -avz hermes.local:dev/sat/artifacts/images/ ~/satprep-backups/images/
```

The figures are a separate line because the archive stores `images` as *path
references*, not content — `question.html` serves them from `artifacts/images`.
A recovery from `backups/` alone would restore every question with figures
pointing at files that are not there. They change only when new material is
ingested, so a periodic pull is enough.

Restoring — the sidecar files matter:

```sh
sudo systemctl stop satprep
rm -f data/satprep.db-wal data/satprep.db-shm
gunzip -c backups/satprep-<stamp>.db.gz > data/satprep.db
sudo systemctl start satprep
```

An unclean shutdown can leave `-wal` and `-shm` behind. They are newer than
the snapshot you just installed, so SQLite replays their frames onto it —
resurrecting the very attempts the restore was meant to discard, or corrupting
the result. Remove them first.

## No authentication

There is none. Anyone who can reach the address can drill, and can read
`/admin` and `/review` — which means the answers are one URL away for anyone
who thinks to look. `satprep serve` binds `127.0.0.1` by default and prints a
notice when told to bind anything else.

This is fine on a home network with one student and no port forwarding. It is
not fine on a network you do not control, and the box should not be exposed to
the internet. If it ever needs to be, put it behind Tailscale rather than
opening a port.

## Checks

```sh
systemctl status satprep                  # running?
journalctl -u satprep -f                  # live log
systemctl list-timers satprep-backup      # next backup
ls -lh backups/                           # snapshots accumulating?
sqlite3 data/satprep.db "SELECT COUNT(*) FROM attempts;"
```
