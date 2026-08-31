---
title: Descriptive title — the tested task and the precise error
type: question-review
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [inference, evidence, scope-error]
question_fingerprint: 64-hex-sha256
student_answer: B
correct_answer: D
confidence: high
---

# <Mirror the frontmatter title>

> Copy this template into `kb/wiki/reviews/<slug>.md` before writing. `question_fingerprint`
> is the stable SHA-256 fingerprint stored on `questions.fingerprint` (see
> `satprep/corpus/fingerprint.py`) — the **join key** to `data/satprep.db`. Do
> **not** paste the canonical question, its choices, the answer rationale, or
> any attempt record into this page; the database owns those.
> Markdown may only add the *diagnosis*.

## Why `question_fingerprint`, not `questions.id`?

`questions.id` is a SQLite `INTEGER PRIMARY KEY` that is reassigned on every
rebuild, import, or row-deletion. `questions.fingerprint` is the normalized
hash of `passage + stem + choices` and is guaranteed unique (`UNIQUE` column
constraint), so an authored review stays bound to the same question across
ingest, backup/restore, and corpus re-exports.

## What was tested

Which task (main idea, inference, words-in-context, …), domain, skill, and
reasoning tag does this question exercise? Cite the relevant question evidence
(stem wording and tags) — not the full question text.

## Why the chosen answer was tempting

Why did `student_answer` look plausible? Commonly: an unsupported strong word, a
reversal of the passage relationship, a true-but-irrelevant statement, a
scope-overreach, or a neighboring-question answer.

## Exact failure

The smallest word, clause, or logical relationship that invalidates the chosen
answer. Quote the exact supporting evidence from the passage that decides it.

## Correct, evidence-based reasoning

Starting from the passage and the question's constraint, walk through why
`correct_answer` follows and the tempting choice does not. Treat third-party
strategy pages as medium-confidence guidance, not College Board authority.

## Links

- Related KB tactic pages: `[[concepts/sat-hard-reading-strategy-stack]]`,
  and the summary page that bears on this error type (e.g.
  `[[summaries/settele-trap-answers]]`).
- Raw transcript or manifest source if the diagnosis traces to a strategy video.

---

Write a review **only** when it captures a reusable diagnosis. If the database
metadata and this page disagree, the database is authoritative.