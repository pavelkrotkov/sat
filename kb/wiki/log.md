# SAT Prep KB Log

This log records changes to the SAT Prep knowledge base vault (`kb/wiki/`) and its
rebuild/deployment. It is maintenance history, not canonical question data — the
database (`data/satprep.db`) remains authoritative for questions, attempts, and
training state.

## [2026-08-30] ingest | six SAT English strategy videos
- Added immutable timestamped transcripts under `raw/transcripts/`.
- Added SHA-256 source manifest at `raw/source-manifest.jsonl` (source URL,
  retrieval timestamp, checksum, content type, byte count, and `unofficial`
  authority label for every source).
- Created six source summaries and one cross-source concept page.
- Sources are labeled `unofficial`; claims are instructional strategies and
  should be tested against hard official College Board questions.

## [2026-08-30] deploy | SAT Prep KB wiki
- Built the rendered MkDocs site from this vault via `kb/rebuild.sh`.
- Added enabled systemd user service `sat-wiki.service` on localhost port 8042.
- Added tailnet route `/sat-wiki` on the controller host.
- Public URL: `https://hermes.tail377b2a.ts.net/sat-wiki/`.

## [2026-08-30] policy | question reviews
- Added `review-templates/question-review.md` and the empty `reviews/` directory.
- An authored review is an explanatory postmortem joined to SQLite by the
  stable `questions.fingerprint` SHA-256 (the autoincrement `questions.id`
  reassigns on rebuild and is **not** a stable join key); it never duplicates
  canonical question or attempt records.