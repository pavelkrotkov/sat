# Question Reviews

Authored, reusable question postmortems live here as `<slug>.md`.

- **How to author:** copy `review-templates/question-review.md` into this directory
  and fill in the frontmatter and the five required sections.
- **Join key:** set `question_id` to the stable SQLite `questions.id` primary
  key from `data/satprep.db`. Do not duplicate the canonical question, choices,
  rationale, or attempt history — Markdown adds only the diagnosis.
- **When to write:** only when a question captures a reusable reasoning error.
  The database is authoritative if this page and SQLite disagree.
- **Discoverability:** add the review to the list in `kb/wiki/index.md`; the
  rebuild copies this directory into the rendered site.

This directory is intentionally empty of authored reviews until the first
reusable diagnosis is recorded.