# SAT Prep Knowledge Base

This directory is the source of truth for the SAT Prep **Markdown knowledge base**
(KB): curated study summaries, a cross-source strategy concept, optional student
question reviews, and the immutable raw video transcripts they are grounded in.
The rendered wiki site is a disposable presentation layer built from this vault.

## Ownership boundaries

| Layer | Location | Authoritative? | Versioned? |
| --- | --- | --- | --- |
| Canonical questions + mutable training state | `data/satprep.db` (SQLite) | **Yes** | No (runtime) |
| Raw evidence + provenance (transcripts, manifest) | `kb/raw/` | Yes | **Yes** |
| Authored knowledge (summaries, concepts, index, reviews) | `kb/wiki/` | Yes | **Yes** |
| Rebuild wrapper + this guide | `kb/rebuild.sh`, `kb/README.md` | Yes | **Yes** |
| Rendered MkDocs site | `kb/build/` (gitignored) or controller host | No | No |
| OpenKB runtime state, generated lint reports | `kb/.openkb/`, `kb/wiki/reports/` | No | No |

The Markdown KB is **explanatory content only**. It may reference a stable SQLite
question id, but it never duplicates or replaces canonical question content,
answers, attempt history, or weakness state. If Markdown metadata and SQLite
disagree, the database wins.

## Layout

```
kb/
  raw/
    source-manifest.jsonl   SHA-256 provenance: source URL, retrieval time,
                            checksum, bytes, authority label per source
    transcripts/            six immutable, timestamped YouTube transcripts
  wiki/
    index.md                navigation hub
    log.md                  maintenance log
    summaries/              one page per source video
    concepts/               cross-source synthesis page
    review-templates/       question-review.md (authoring convention)
    reviews/                authored question reviews (join to SQLite by id)
  rebuild.sh                path-independent MkDocs build wrapper
  README.md                 this file
```

## Prerequisites

- Python 3 with the shared research-fabric wiki builder:
  `research-fabric/tools/wiki/build_wiki.py` (defaults to
  `/home/pavel/research-fabric/tools/wiki/build_wiki.py`; override with
  `SAT_WIKI_BUILDER`).
- `mkdocs` with the `material` theme on `PATH` (override with `SAT_WIKI_MKDOCS`).
- No database connection is needed to build the KB; `data/satprep.db` is only
  used to look up stable question ids when authoring reviews.

## Rebuild

Run from any clone:

```bash
./kb/rebuild.sh
```

The site is written to the gitignored `kb/build/site-src/site/`. To point at a
different builder or mkdocs:

```bash
SAT_WIKI_BUILDER=/path/to/build_wiki.py SAT_WIKI_MKDOCS=/path/to/mkdocs ./kb/rebuild.sh
```

If a prerequisite is missing the script fails immediately with a clear message;
nothing is written to `kb/wiki/` or tracked by Git.

## Authoring a new summary or source

1. Add the immutable transcript under `kb/raw/transcripts/` and a matching
   provenance row to `kb/raw/source-manifest.jsonl` (keep source URL, retrieval
   timestamp, checksum, content type, byte count, and authority label).
2. Add a page under `kb/wiki/summaries/` with `type: summary` frontmatter whose
   `sources` list references the committed transcript path, and `authority`
   matching the manifest.
3. Link the page from `kb/wiki/index.md`.
4. Rebuild with `./kb/rebuild.sh` and confirm the wikilinks resolve.

## Question reviews

A question review is an **explanatory postmortem** of one question, kept in
`kb/wiki/reviews/<slug>.md`. It joins to SQLite by the stable
`questions.fingerprint` SHA-256 (`satprep/corpus/fingerprint.py`) — it does
not copy the canonical question, answers, or attempt record. The fingerprint
is the only stable join key: `questions.id` is an autoincrement primary key
that is reassigned on rebuild, ingest, or restore. See
`kb/wiki/review-templates/question-review.md` for the frontmatter schema and
required sections. Write a review only when it captures a reusable diagnosis;
the database remains authoritative on disagreement.

> **Why `review-templates/` and not `templates/`?** MkDocs reserves a
> top-level `templates/` directory for custom theme overrides and excludes it
> from the built site by default, so a page there could never render. The
> review-templates directory keeps the authoring convention discoverable in the
> rendered navigation.

## Deployment (controller host)

Optional hosting; the KB is fully usable as Markdown locally. On the controller
the service serves from a document root that is **separate** from this
checkout, so the rebuild must publish the freshly built site to that root
before the service restart:

```bash
# Build AND publish into the service document root:
SAT_WIKI_DEPLOY=/home/pavel/services/sat-wiki/site-src/site ./kb/rebuild.sh

# Restart the user service and verify:
systemctl --user restart sat-wiki.service
systemctl --user status sat-wiki.service
curl --fail http://127.0.0.1:8042/
curl --fail https://hermes.tail377b2a.ts.net/sat-wiki/
```

The systemd unit serves the built site with `python -m http.server 8042`
(bound to loopback); the tailnet route exposes `/sat-wiki`. `SAT_WIKI_DEPLOY`
defaults to empty (build only); on a hosted controller set it to the service
document root so the rebuild always copies fresh output. The service URL is
optional infrastructure, not a requirement for the trainer or local reading.

## Verification

```bash
./kb/rebuild.sh                                  # builds; fails loudly on missing prereqs OR KB lint failure
git status                                       # no kb/build, kb/.openkb, or kb/wiki/reports changes
uv run pytest -q                                 # trainer behaviour unchanged
scripts/check_kb.py                              # KB lint: frontmatter, manifest, wikilinks, index
```

Additive checks before merging changes: every `sources` entry must resolve to a
committed transcript, every `[[wikilink]]` must resolve, and
`kb/raw/source-manifest.jsonl` must keep all six source URLs, retrieval
timestamps, and checksums. CI runs `scripts/check_kb.py` on every PR; a
broken link, missing source, invalid required frontmatter, duplicate review
reference, or build failure blocks the PR with the same actionable message.

## Retrieval index (`kb/.kb-index.json`)

The lint script emits a deterministic JSON index of every authored page and
raw source, used by the KB-aware error-explanation pipeline (issue #36) for
retrieval instead of re-walking the vault at every call. The index is
**deliberately versioned** (not gitignored) for two reasons:

1. Diffability: a PR that changes vault metadata is a real change, and the
   reviewer should see the index move in lockstep.
2. LLM pipeline stability: issue #36 reads the committed index instead of
   re-running the lint on every call. A versioned index means a stale read
   is at least a known-stale read.

`scripts/check_kb.py` always regenerates `kb/.kb-index.json` and the build
rejects a missing or stale index. If the lint pass produces a different
index than the committed one, the lint output still says "wrote …" — to
fail the build on a stale committed index, run `scripts/check_kb.py --check`
(in CI) rather than the default mode.