# Implementation Plan: Integrate the SAT Prep Knowledge Base

Tracks [issue #35](https://github.com/pavelkrotkov/sat/issues/35).

## Outcome

Make this repository the source of truth for the SAT Prep Markdown knowledge base (KB). A new contributor should be able to distinguish the KB from trainer data, inspect source provenance, rebuild the rendered wiki, and add a student question review without changing canonical question or attempt data.

This work integrates and documents the existing KB. Building an automated error classifier, retrieval index, personalized remediation engine, or SQLite-to-Markdown synchronization is out of scope for issue #35.

## Source-of-truth policy

| Content | Source of truth | Versioned? |
| --- | --- | --- |
| Curated summaries, concepts, index, and maintenance log | `kb/wiki/` | Yes |
| Question-review template and intentionally authored reviews | `kb/wiki/templates/` and `kb/wiki/reviews/` | Yes |
| Immutable transcript snapshots and source manifest | `kb/raw/` | Yes |
| Reproducible KB configuration and rebuild wrapper | `kb/` | Yes |
| OpenKB locks, caches, hashes, and generated lint reports | `kb/.openkb/` runtime files and `kb/wiki/reports/` | No |
| Generated MkDocs docs/site, service state, and logs | Controller host (currently `/home/pavel/services/sat-wiki/`) | No |
| Questions, attempts, sessions, scores, and weakness state | `data/satprep.db` | No; runtime data |

The Markdown KB is explanatory content. It may reference a stable question identifier, but it must not duplicate or replace canonical question content, answers, attempt history, or weakness state stored in SQLite.

## Phase 1: Normalize the repository layout

1. Audit every file under `kb/` and retain the six transcripts, `raw/source-manifest.jsonl`, six summary pages, synthesis concept page, `index.md`, and useful portions of `log.md`.
2. Remove host-specific absolute paths from versioned metadata where they are not needed for provenance. Preserve source URL, retrieval timestamp, checksum, content type, byte count, source ID, and authority classification.
3. Version reproducible OpenKB configuration if required to maintain the vault. Ignore mutable files such as `ingest.lock` and `hashes.json`.
4. Ignore `kb/wiki/reports/` and all generated MkDocs content. Do not commit `/home/pavel/services/sat-wiki/` or copy its `site/` directory into the repository.
5. Add placeholder files only where Git must retain an intentionally empty authored-content directory, such as `kb/wiki/reviews/`.

Deliverable: a self-contained `kb/` tree whose committed files are authored knowledge, immutable evidence, provenance metadata, configuration, or documentation.

## Phase 2: Make rebuilding explicit

1. Add `kb/README.md` as the maintenance guide.
2. Add a repository-owned `kb/rebuild.sh` based on the working controller-host script. It should:
   - resolve repository and vault paths relative to the script instead of hard-coding `/home/pavel/dev/sat`;
   - accept the shared `research-fabric/tools/wiki/build_wiki.py` path through an argument or documented environment variable;
   - write MkDocs input and output outside the versioned vault or into an ignored build directory;
   - fail clearly when the shared builder or MkDocs environment is unavailable.
3. Document prerequisites and one exact local rebuild command in `kb/README.md`.
4. Document that the controller deployment serves the site with `sat-wiki.service` on `127.0.0.1:8042`, with a tailnet route at `https://hermes.tail377b2a.ts.net/sat-wiki/`.
5. Document deployment and verification commands:

   ```bash
   ./kb/rebuild.sh
   systemctl --user restart sat-wiki.service
   systemctl --user status sat-wiki.service
   curl --fail http://127.0.0.1:8042/
   curl --fail https://hermes.tail377b2a.ts.net/sat-wiki/
   ```

The hosted URL is optional infrastructure, not a requirement for running the trainer or reading the Markdown locally.

Deliverable: a contributor-facing maintenance guide and path-independent rebuild entry point; generated artifacts remain untracked.

## Phase 3: Document how the trainer and KB fit together

Add a concise “SAT Prep knowledge base” section to the top-level `README.md` covering:

1. The four-layer boundary:
   - `data/satprep.db`: canonical questions and mutable training state;
   - `kb/raw/`: immutable source evidence and provenance;
   - `kb/wiki/`: authored explanatory knowledge and optional reviews;
   - generated MkDocs output: disposable presentation layer.
2. Links to `kb/wiki/index.md`, `kb/raw/source-manifest.jsonl`, and `kb/README.md`.
3. The exact local rebuild command and optional hosted URL.
4. A short LLM usage contract:
   - start from the passage, question, answers, official rationale, and relevant SQLite tags;
   - classify the smallest observable reasoning error;
   - retrieve only relevant KB pages by frontmatter tags and wikilinks;
   - cite exact question evidence and identify the word or relationship that invalidates the chosen answer;
   - treat third-party strategy pages as medium-confidence guidance, not College Board authority;
   - never invent a classification when evidence is insufficient.
5. A stable explanation shape: what was tested, why the wrong choice was tempting, exact failure, correct evidence-based reasoning, and linked KB tactic.

Deliverable: the repository README makes the KB discoverable without obscuring the trainer documentation.

## Phase 4: Define question reviews without duplicating trainer data

1. Add `kb/wiki/templates/question-review.md` with documented frontmatter fields:

   ```yaml
   ---
   title: Unsupported scope in inference question
   type: question-review
   created: YYYY-MM-DD
   updated: YYYY-MM-DD
   tags: [inference, evidence, scope-error]
   question_id: stable-sqlite-question-id
   student_answer: B
   correct_answer: D
   confidence: high
   ---
   ```

2. Give the body fixed sections for tested task, tempting-answer diagnosis, exact failure, supporting evidence, corrected reasoning, and related KB links.
3. Document how to copy the template into `kb/wiki/reviews/`, use the stable SQLite question ID as the join key, and avoid copying the full canonical question or attempt record into Markdown.
4. State that a review is written only when it contributes a reusable diagnosis. The database remains authoritative if Markdown metadata and SQLite disagree.
5. Update `kb/wiki/index.md` or the rebuild tooling so authored reviews are discoverable in the rendered site.

Deliverable: contributors and LLMs have one auditable convention for explanatory postmortems.

## Phase 5: Verify and deliver

1. Confirm all six source URLs, retrieval timestamps, and checksums remain in `kb/raw/source-manifest.jsonl`.
2. Check that each summary's `sources` entry resolves to a committed transcript and all wikilinks resolve.
3. Run the KB lint/build workflow and inspect navigation for the index, summaries, concept page, template, and reviews.
4. Confirm generated site files, OpenKB runtime state, and lint reports do not appear in `git status`.
5. Run the existing trainer suite:

   ```bash
   uv run pytest -q
   ```

6. Smoke-test the local site and, when deploying from the controller, the systemd service and tailnet URL.
7. Run `git diff --check` and inspect the tracked-file list for runtime data or machine-specific absolute paths.
8. Create a feature branch, commit the integration, push the branch, and open a PR with `Closes #35`. Do not push to `main`.

## Completion checklist

- [ ] The repository is the documented source of truth for KB Markdown and provenance.
- [ ] Raw transcripts and manifest provenance are committed and internally consistent.
- [ ] Generated site output and runtime state are excluded.
- [ ] A fresh clone has documented prerequisites and an exact rebuild command.
- [ ] The README separates SQLite, raw evidence, Markdown knowledge, and generated presentation.
- [ ] LLM classification/explanation guidance is documented without adding an unrequested runtime subsystem.
- [ ] The question-review template uses a stable database ID and does not duplicate trainer data.
- [ ] KB lint/build verification succeeds.
- [ ] Existing tests pass.
- [ ] Work is delivered through a feature branch and PR that closes issue #35.
