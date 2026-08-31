# SAT R&W Trainer (satprep)

Local-first, personalized SAT Reading & Writing training built on top of this repo's
scraped Bluebook history. **Train the weakness, not the remembered question.**

Part 1 of this README documents the original scraper; satprep documentation follows.

---

## satprep

### Architecture

```
raw sources (read-only)          generated state
----------------------------     ------------------------------------------
outputs/wrong_questions.json --> data/satprep.db (SQLite canonical store)
artifacts/html/*.html        -->   questions (normalized, fingerprinted)
                                   attempts  (history + in-app sessions)
imports/*.csv|json (CB bank) -->   tags, weaknesses, sessions, traces
```

The package mirrors that split:

```
satprep/corpus/    questions, tags, pools, fingerprints, archive  — rebuildable
satprep/training/  attempts, sessions, spacing, weakness model    — irreplaceable
satprep/           config · db · clock · ids · analytics · cli · server
```

`corpus` never imports `training`; the dependency runs one way, enforced by
`tests/test_module_boundaries.py`.

Stored questions are hydrated once into a `Question` value
(`satprep/corpus/questions.py`) rather than passed around as `sqlite3.Row`,
so `choices_json` is decoded in one place and a missing column fails at the
seam. It is deliberately distinct from `ParsedQuestion`, which is a
source-specific parse result carrying the student's answer.

* `satprep/corpus/ingest.py` rebuilds the corpus from raw sources; **idempotent** — reruns
  never duplicate questions or attempt history.
* Questions are fingerprinted by normalized `passage + stem + choices`
  (`satprep/corpus/fingerprint.py`), so duplicates across exports/banks collapse.
* Fresh Question Bank items split deterministically ~75/25 into
  `fresh_training` / `protected_benchmark` (`pool_for_fingerprint`). Protected
  items are excluded at the query level from every mode except Fresh Benchmark,
  and enter the normal pool only after being answered there.
* Two-level taxonomy: official CB skills (metadata or deterministic stem rules,
  never guessed) plus a granular reasoning-tag layer (`satprep/corpus/tagger.py`,
  `satprep/config.py`). Rule-based tagging is cached in SQLite; an
  `llm_tag_cache` table exists for optional out-of-band LLM classification;
  manual corrections via the admin UI always win. Tag precedence and
  suppression live in `satprep/corpus/tags.py` — the only module that reads
  `question_tags.origin`; everything else goes through `effective_tags` /
  `tags_by_question` or joins the `effective_question_tags` view, so a
  suppressed tag disappears from the sampler and the weakness model too.
* Weakness model (`satprep/training/weakness.py`): recency-decayed, confidence-weighted
  Bayesian error rate per skill/tag with evidence shrinkage, difficulty bonus,
  and a mastery discount. Uses ALL historical questions, not just errors.
* Sampler (`satprep/training/sampler.py`): additive, fully explainable weights; every drill
  stores its seed, algorithm version, chosen IDs and per-question score breakdown.
  Bucket rules, the largest-remainder allocator and the graceful-fill rule live in
  `satprep/training/composition.py` — pure, so drill composition is testable
  without a database.
* Spacing (`satprep/training/spacing.py`): SM-2-lite intervals; confidently-wrong → soonest,
  confidently-correct → longest; exact repeats yield to same-tag different-question.

### Install & run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # create venv from pyproject.toml
uv run satprep ingest        # build/rebuild corpus from outputs/ + imports/
uv run satprep analyze       # compute weakness profile
uv run satprep serve         # web UI on http://127.0.0.1:8765
uv run satprep serve --host 0.0.0.0   # reachable on the local network
```

CLI: `ingest | analyze | drill [--count N] [--mode M] [--focus TAG] | benchmark |
stats | serve`. Web UI and CLI share the same DB and selection logic.

The UI has no authentication, so `serve` binds loopback by default and prints a
notice when told to bind anything else. To run it on an always-on box that other
devices on the network can reach, see [deploy/README.md](deploy/README.md) —
systemd units, mDNS, one-directional corpus sync and nightly backups. The rule
there is that the serving box owns `data/satprep.db`: attempt history lives only
in that file, and the archive deliberately does not carry it.

### Ingesting new official material

**Automated (preferred):** the College Board Educator Question Bank is public
(no login). satprep talks to its JSON API directly:

```bash
uv run satprep fetch-qbank                  # all SAT R&W items (~1.8k)
uv run satprep fetch-qbank --hard-only      # only CB-marked Hard items
uv run satprep fetch-qbank --domains INI,CAS
```

Each import becomes a dated batch (`eqb-YYYYMMDD`); reruns skip already-stored
`external_id`s, so it is fully resumable/idempotent.

**Manual:** drop official exports (CSV/JSON) into:

```
imports/          # gitignored
```

Supported keys (case-insensitive): `passage/stimulus`, `stem/question`,
`choices/answer_options`, `correct/correct_answer/answer_key`, `domain`, `skill`,
`difficulty`, `rationale/explanation`. Each file becomes a batch; fingerprints are
matched against everything already stored, so bank questions overlapping the eight
practice tests are not double-counted as fresh. The custom PDFs under `cram_claude/`
are third-party approximations and are deliberately *not* ingested as official.

Note: Bluebook's review view omits answer options for correctly-answered questions,
so 425 historical questions are stats-only (no choice text stored anywhere in this
repo). They still inform the weakness model but cannot be displayed until choice
data is imported from an official source.

### Screens

Dashboard · Start Drill · Question (passage/A-D/confidence/timer) · Results ·
Review Mistakes · Weakness Analysis · History · Fresh Benchmark · Admin
(selection traces "why was this picked?" + tag inspector/corrector).

Drill modes: **Targeted Drill** (12q, 4 old-miss / 3 correct-but-relevant /
5 fresh targets, gracefully degraded), **Error Clinic**, **Transfer Drill**
(no memorized errors), **Hard Mixed Module** (27q), **Fresh Benchmark**.

### How selection works

Each candidate accumulates transparent components, e.g.:
`weak-tag:hypothesis_vs_result +1.2`, `fresh-matching-weak-tags +2.5`,
`difficulty:hard +1.2`, `exposure-penalty −1.1` → total weight. Inspect any
session at `/admin/why/<session_id>`.

### Backup

Two-layer model:

- **Training state** (attempts, sessions, spacing) lives only in
  `data/satprep.db` — copy that file (plus `-wal` if present) to back it up.
- **Question content** is additionally snapshotted to `exports/corpus-v1.jsonl`,
  rewritten automatically after every ingest/fetch. A fresh database can be
  rebuilt from the archive alone:

```bash
uv run satprep restore            # rebuild questions from exports/corpus-v1.jsonl
uv run satprep export --out path.jsonl   # manual snapshot anywhere you like
```

Raw sources (`outputs/`, `artifacts/`) remain untouched provenance; a full
rebuild from them stays possible via `rm data/satprep.db && uv run satprep ingest`.

### Tests

```bash
uv run pytest -q
```

Includes leakage tests hammering every mode × 25 seeds asserting protected
benchmark questions can never appear outside benchmark mode.

### SAT Prep knowledge base

A curated Markdown knowledge base (`kb/`) sits alongside the trainer as an
explanatory layer. It holds strategy summaries and a cross-source concept page
grounded in six immutable video transcripts, plus optional per-question reviews.
The rendered site is a disposable presentation layer.

There are four distinct layers with separate owners:

| Layer | Location | Authoritative for |
| --- | --- | --- |
| SQLite training state | `data/satprep.db` | Canonical questions, attempts, sessions, scores, weakness state |
| Raw evidence + provenance | `kb/raw/` | Immutable transcripts, source manifest (URL, checksum, authority) |
| Authored knowledge | `kb/wiki/` | Summaries, concepts, index, optional question reviews |
| Rendered wiki site | `kb/build/` (gitignored) / controller host | Disposable presentation only |

See [kb/README.md](kb/README.md) for ownership boundaries, prerequisites, and
maintenance; [kb/wiki/index.md](kb/wiki/index.md) is the navigation hub; and
[kb/raw/source-manifest.jsonl](kb/raw/source-manifest.jsonl) records provenance.

Rebuild the site from any clone:

```bash
./kb/rebuild.sh          # writes to gitignored kb/build/site-src/site
```

As of this writing the controller host serves the site at
`https://hermes.tail377b2a.ts.net/sat-wiki/`; that URL is optional
infrastructure, not a requirement for the trainer.

**How an LLM should use the KB to explain an error** (documented contract):

1. Start from the passage, question, answer choices, official rationale, and the
   relevant SQLite tags.
2. Classify the smallest observable reasoning error (unsupported strong word,
   scope overreach, reversal, irrelevant truth, …) and cite exact question
   evidence.
3. Retrieve only relevant `kb/wiki/` pages, by frontmatter tags and wikilinks.
4. Explain the exact failure: quote the word or relationship that invalidates
   the student's answer, then give the correct evidence-based reasoning and
   link the KB tactic that applies.
5. Treat third-party strategy pages as medium-confidence guidance, never
   College Board authority, and never invent a classification when the evidence
   is insufficient.

The stable explanation shape is: what was tested → why the wrong choice was
tempting → exact failure → correct evidence-based reasoning → linked KB tactic.
Authored postmortems follow the convention in
[kb/wiki/review-templates/question-review.md](kb/wiki/review-templates/question-review.md) and
are joined to SQLite by the stable `questions.fingerprint` SHA-256 (not the
autoincrement `id`) without duplicating canonical data.

---

# Original scraper docs

## SAT Bluebook Wrong-Question Scraper

This repo contains a local Playwright scraper for College Board My Practice. It opens Chromium, pauses for manual login when needed, walks SAT Practice Tests, captures incorrect-question review pages, and exports multiple report formats.

## Main script

- `scrape_wrong_questions.py`: single-file scraper runnable with `uv`
- `export_md_to_pdf.sh`: render a Markdown file to standalone HTML with Pandoc, then print it to PDF with headless Chrome/Chromium

## Outputs

Generated under `outputs/`:

- `wrong_questions.json`: structured source-of-truth dataset
- `wrong_questions.csv`: flat export
- `wrong_questions.md`: human-oriented Markdown report with embedded figure references
- `wrong_questions.llm.md`: LLM-oriented standalone Markdown with math preserved and figures converted to text descriptions
- `wrong_questions.html`: standalone HTML export of `wrong_questions.md` when `pandoc` is installed
- `drill_pack.md`: condensed drill sheet
- `drill_pack.html`: standalone HTML export of `drill_pack.md` when `pandoc` is installed
- `pandoc-report.css`: generated copy of the repo-level stylesheet used for standalone HTML export

Generated under `artifacts/`:

- `html/`: per-question HTML snapshots used for parser fixes and rebuilds
- `images/`: extracted SVG or figure assets
- `page_visits/`: full-page navigation snapshots for debugging live runs, only when `--save-page-visits` is enabled
- `errors/`: failure screenshots when the live scrape hits an unexpected state, only when `--save-error-screenshots` is enabled
- `screenshots/`: per-question screenshots, only when `--save-question-screenshots` is enabled

Most generated/debug files are ignored by Git via `.gitignore`.

## What it does

1. Launches Chromium with a persistent profile in `playwright_profile/` by default
2. Waits for manual login if the current session is not already on a recognizable My Practice screen
3. Finds SAT Practice Tests on the My Practice dashboard
4. Opens each test, navigates to score details, switches to the full question list, and keeps rows marked `Incorrect`
5. Opens each review view, turns on `Show correct answer and explanation`, and extracts:
   - metadata such as section, module, domain, skill, and answer status
   - question/explanation rich HTML for math-preserving report generation
   - per-question HTML snapshots and figure assets
6. Checkpoints JSON after each question for crash recovery, then renders the full report set once at the end using batched Pandoc conversions if `pandoc` is available

Run it with `--all-questions` to capture every row (correct rows get an
`answer_status` field too), which is what fed satprep's corpus.
