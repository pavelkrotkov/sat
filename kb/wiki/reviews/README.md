# Question Reviews

Authored, reusable question postmortems live here as `<slug>.md`.

- **How to author:** copy `review-templates/question-review.md` into this directory
  and fill in the frontmatter and the five required sections.
- **Join key:** set `question_fingerprint` to the stable SHA-256 stored in
  `data/satprep.db`'s `questions.fingerprint` column
  (`satprep/corpus/fingerprint.py`). `questions.id` is an autoincrement PK
  that is reassigned on rebuild, so it is not a stable join key. The
  fingerprint is `UNIQUE` and survives ingest, backup/restore, and corpus
  re-exports. Do not duplicate the canonical question, choices, rationale, or
  attempt history — Markdown adds only the diagnosis.
- **When to write:** only when a question captures a reusable reasoning error.
  The database is authoritative if this page and SQLite disagree.
- **Discoverability:** add the review to the list in `kb/wiki/index.md`; the
  rebuild copies this directory into the rendered site.

This directory is intentionally empty of authored reviews until the first
reusable diagnosis is recorded.