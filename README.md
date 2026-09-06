# SAT R&W Trainer (`satprep`)

Local-first SAT Reading & Writing training software. This repository is intended
to contain **source code only**: it does not redistribute SAT questions, answer
keys, student history, scraped review pages, or a question database.

SAT and College Board are trademarks of their respective owner. This project is
independent and unaffiliated; those names are used only to describe compatible
workflows and source material that a user may choose to supply locally.

## Data stays local

Question text, answer data, attempts, scores, generated reports, imports,
exports, browser state, backups, and other runtime material belong on the user's
machine and are gitignored. See [`docs/data-boundary.md`](docs/data-boundary.md)
for the exact public/private boundary and publication checks.

The main local paths are:

```text
data/          SQLite runtime state
imports/       user-supplied permitted imports
outputs/       scraper/export output
artifacts/     HTML/image/debug snapshots
exports/       corpus snapshots
backups/       database backups
kb/            local/private knowledge and review material
```

Do not commit those directories or generated question files elsewhere in the
tree. Pre-commit and CI run `scripts/check_public_repo.py` to enforce that rule.

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run satprep ingest
uv run satprep analyze
uv run satprep serve
```

The web UI binds to `127.0.0.1:8765` by default. It has no authentication; use
`--host 0.0.0.0` only on a network you trust. Generic systemd deployment and
backup templates are in [`deploy/`](deploy/README.md).

CLI commands include `ingest`, `analyze`, `drill`, `benchmark`, `stats`,
`serve`, `fetch-qbank`, `explain`, `review`, and `remediate`.

## Supplying your own permitted data

The application supports local inputs without requiring them to be checked into
Git:

- place supported CSV/JSON imports in `imports/`;
- run the optional local Playwright scraper to populate `outputs/` and
  `artifacts/` from an account you are authorized to use;
- use `satprep fetch-qbank` where access to the upstream source and downstream
  use are permitted.

Users are responsible for having the right to access and use any question or
answer material they supply. The repository license covers the code, not
third-party test content.

## Architecture

```text
local sources                    local generated state
-----------------------------    -----------------------------
outputs/ + artifacts/  ------->  data/satprep.db
imports/               ------->  questions, attempts, tags,
                                 weaknesses, sessions, traces
```

Source modules are split between `satprep/corpus/` (question ingestion,
fingerprints, archives and tags), `satprep/training/` (selection, sessions,
spacing and weakness modelling), and the top-level application/CLI/server
modules. Runtime data is not needed to build or test the package.

## Optional Bluebook scraper

`scrape_wrong_questions.py` is a local Playwright scraper for College Board My
Practice. It pauses for user login, visits review pages, and writes generated
material only to ignored local directories. `export_md_to_pdf.sh` can render a
local Markdown report with Pandoc/Chromium.

No scraper output or browser profile belongs in this repository.

## Development

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run ty check --extra-search-path . .
python scripts/check_public_repo.py
```

Pre-commit runs formatting/lint checks plus the public-repository leak guard.
CI also runs Gitleaks as a dedicated secret scan.

## License

The project code is licensed under the [MIT License](LICENSE). Third-party SAT
questions, explanations, exports, transcripts, and other content are not part
of this distribution and are not licensed by this repository.
