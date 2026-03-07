# Refactor Plan

## Goal

Streamline `scrape_wrong_questions.py` now that the scraper has completed a successful end-to-end run and the core workflow is understood.

The refactor should reduce:

- hot-path I/O during a live scrape
- duplicated parsing and rendering logic
- always-on debug behavior
- dead or low-value code paths

It should preserve:

- successful live scraping
- rebuilds from saved HTML snapshots
- the current output set, especially `wrong_questions.json` and `wrong_questions.llm.md`

## Current issues

### 1. Full output regeneration happens on every question

`OutputManager.upsert()` saves immediately after each record. `save()` then:

- refreshes record assets for the entire dataset
- rewrites JSON
- rewrites CSV
- rewrites both Markdown reports
- rewrites the drill pack
- rewrites CSS

That makes the live scrape more expensive than necessary and scales poorly as the dataset grows.

### 2. Debug page snapshots are always enabled

The scraper currently records page-visit HTML snapshots on:

- page attach
- `domcontentloaded`
- `load`
- main-frame navigation
- `wait_for_ready_state()`

This was useful while stabilizing the scraper, but it should not remain on the default path.

### 3. Two parsing systems run for every review page

Each review page currently uses:

- structured DOM extraction
- raw visible text extraction
- raw-text parsing heuristics

The structured HTML extractor is now the higher-value path. The raw-text parser should become a fallback, not a first-class parallel parser.

### 4. Repeated waits and repeated table rescans

There are multiple places where the scraper waits twice after the same action or rescans the full questions table repeatedly inside the question loop.

This adds latency and complexity without improving successful-run behavior.

### 5. Renderer and conversion logic is duplicated

There is duplicated structure between:

- `_render_markdown()`
- `_render_llm_markdown()`

And also between:

- `_html_fragment_to_markdown()`
- `_html_fragment_to_plain()`

This increases maintenance cost and makes later changes error-prone.

### 6. Some methods and fields look dead or low-value

Examples:

- `process_test_by_name()` appears unused
- `review_modal_is_visible()` appears unused
- some record fields such as `review_url`, `notes`, and `raw_visible_text` may be archival rather than operational

These should be reviewed and either removed or clearly justified.

### 7. Parsing and browser-driving concerns are mixed together

`SatBluebookScraper` currently owns:

- browser control
- navigation/state detection
- review parsing
- artifact capture

The rebuild path also instantiates the scraper just to reuse parsing logic. That is a sign the parser wants to be separated from browser automation.

## Refactor phases

## Phase 1: Remove default debug overhead

### Objective

Make debug capture opt-in instead of default.

### Changes

- Add explicit CLI flags for debug capture, for example:
  - `--save-page-visits`
  - `--save-question-screenshots`
  - `--save-error-screenshots`
- Only create `artifacts/page_visits/` when page-visit capture is enabled.
- Stop calling `snapshot_page()` from `wait_for_ready_state()` unless debug capture is enabled.
- Keep `artifacts/html/` and `artifacts/images/` as the default artifact set because they support rebuilds and parser fixes.

### Expected outcome

- Much lower disk usage during normal runs
- Less I/O and fewer writes on the live path
- Cleaner default behavior for future use

## Phase 2: Split incremental persistence from final report generation

### Objective

Keep crash recovery, but stop regenerating the whole report set after every question.

### Changes

- Change `upsert()` so it only updates in-memory records.
- Introduce two save levels:
  - lightweight incremental save: JSON only
  - full finalize save: JSON, CSV, Markdown, drill pack, HTML
- Optionally checkpoint JSON every question and full reports every test.
- Move `_refresh_record_assets()` so it only runs:
  - when a record is first created
  - or during rebuild/finalize

### Expected outcome

- Live scrape cost becomes closer to O(n)
- Faster runs
- Less repeated Pandoc work and less repeated HTML rereading

## Phase 3: Make structured HTML parsing primary

### Objective

Treat the structured DOM extractor as the main parser and downgrade raw-text parsing to fallback status.

### Changes

- In `scrape_review_page()`, parse with `extract_review_structured_data()` first.
- Only run `extract_visible_text()` plus `parse_review_content()` if required fields are still missing.
- Define a small set of required fields, for example:
  - `question_html` or `question_text`
  - `explanation_html` or `explanation`
  - `correct_answer`
- Revisit whether `raw_visible_text` is still needed in stored records.

### Expected outcome

- Cleaner parsing logic
- Fewer heuristic misparses
- Simpler reasoning about what the scraper trusts

## Phase 4: Simplify navigation and table handling

### Objective

Reduce repeated waits and repeated scans now that the workflow is known.

### Changes

- Remove duplicate `wait_for_ready_state()` calls after `click_with_possible_popup()` where the helper already waits.
- Compute the incorrect row list once after `set_view_all()` and iterate over stable identifiers rather than recomputing the full row index list every time.
- Tighten `set_view_all()` to the selectors that actually worked in the successful runs, keeping one fallback path instead of several overlapping ones.
- Review whether `lazy_load()` is still needed for the current dashboard behavior.

### Expected outcome

- Lower latency per test
- Less control-flow complexity
- Fewer accidental race conditions

## Phase 5: Consolidate report generation

### Objective

Reduce duplication in the reporting layer while preserving the distinct outputs.

### Changes

- Build a shared question-section renderer with mode switches for:
  - normal report
  - LLM report
- Build one generalized fragment converter that accepts a target format (`commonmark_x` or `plain`) instead of maintaining separate near-identical functions.
- Keep the LLM report’s visual-context extraction, but move it behind a cleaner interface.
- Make `drill_pack` generation independent from scrape-time fields that may be removed later.

### Expected outcome

- Smaller report layer
- Easier maintenance when output wording changes
- Lower risk of one report drifting from another unintentionally

## Phase 6: Remove dead code and trim record schema

### Objective

Delete code and fields that no longer justify their existence.

### Changes

- Remove unused methods after confirming they are not referenced:
  - `process_test_by_name()`
  - `review_modal_is_visible()`
- Audit record fields and drop low-value ones if they are not needed for:
  - reporting
  - subject detection
  - rebuilds
  - debugging
- Candidate fields to review:
  - `review_url`
  - `notes`
  - `raw_visible_text`
  - `source_row_text`

### Expected outcome

- Smaller schema
- Less incidental complexity
- Cleaner serialized data

## Phase 7: Separate parsing from browser automation

### Objective

Make rebuild logic independent from the live scraper controller.

### Changes

- Extract review parsing into a dedicated component, for example:
  - `ReviewParser`
  - or a small group of pure/helper functions
- Keep browser-only responsibilities inside `SatBluebookScraper`.
- Let rebuild mode call the parser directly instead of constructing the full scraper just to parse saved HTML.

### Expected outcome

- Cleaner architecture
- Easier testing
- Easier future parser iteration without touching navigation code

## Implementation order

Recommended order:

1. Phase 1: disable default debug overhead
2. Phase 2: split lightweight save vs finalize
3. Phase 3: make structured parsing primary
4. Phase 4: simplify navigation/table iteration
5. Phase 6: remove dead code and trim schema
6. Phase 5: consolidate report generation
7. Phase 7: split parser from browser automation

This order keeps risk controlled:

- first remove obvious runtime overhead
- then simplify the scrape path
- then clean up architecture once behavior is stable

## Validation plan

After each phase:

- run `python3 -m py_compile scrape_wrong_questions.py`
- run rebuild mode against the current saved dataset:
  - `uv run scrape_wrong_questions.py --rebuild-from-json outputs/wrong_questions.json`
- verify that:
  - `wrong_questions.json` still loads cleanly
  - `wrong_questions.llm.md` still preserves math and visual text context
  - `wrong_questions.md` still renders figures correctly
  - standalone HTML export still works when `pandoc` is installed

Before removing any record field:

- confirm it is not used by:
  - `detect_subject()`
  - drill-pack generation
  - rebuild mode
  - any report renderer

Before removing any browser fallback:

- test at least one live scrape path through:
  - dashboard
  - test details
  - questions overview
  - review modal

## Non-goals

This refactor should not:

- redesign the output formats
- remove rebuild-from-snapshots support
- optimize for concurrent scraping
- add new site coverage outside the currently working SAT Practice Test flow

## Success criteria

The refactor is successful if:

- normal runs no longer generate heavy debug artifacts by default
- per-question processing is materially faster
- rebuild mode still works
- the code is smaller and easier to reason about
- the parser/report pipeline has fewer overlapping paths
