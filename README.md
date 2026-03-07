# SAT Bluebook Wrong-Question Scraper

This repo contains a local Playwright scraper for College Board My Practice. It opens Chromium, pauses for manual login when needed, walks SAT Practice Tests, captures incorrect-question review pages, and exports multiple report formats.

## Main script

- `scrape_wrong_questions.py`: single-file scraper runnable with `uv`

## Outputs

Generated under `outputs/`:

- `wrong_questions.json`: structured source-of-truth dataset
- `wrong_questions.csv`: flat export
- `wrong_questions.md`: human-oriented Markdown report with embedded figure references
- `wrong_questions.llm.md`: LLM-oriented standalone Markdown with math preserved and figures converted to text descriptions
- `wrong_questions.html`: standalone HTML export of `wrong_questions.md` when `pandoc` is installed
- `drill_pack.md`: condensed drill sheet
- `drill_pack.html`: standalone HTML export of `drill_pack.md` when `pandoc` is installed
- `pandoc-report.css`: CSS used for standalone HTML export

Generated under `artifacts/`:

- `html/`: per-question HTML snapshots used for parser fixes and rebuilds
- `images/`: extracted SVG or figure assets
- `page_visits/`: full-page navigation snapshots for debugging live runs
- `errors/`: failure screenshots when the live scrape hits an unexpected state
- `screenshots/`: per-question screenshots

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
6. Writes outputs incrementally after each question and renders standalone HTML at the end if `pandoc` is available

## Setup

Install the Playwright browser once:

```bash
uv run --with playwright python -m playwright install chromium
```

Optional but recommended for standalone HTML export:

```bash
brew install pandoc
```

## Usage

Run the live scraper:

```bash
uv run scrape_wrong_questions.py
```

Useful flags:

```bash
uv run scrape_wrong_questions.py --slow-mo 200 --max-tests 2
uv run scrape_wrong_questions.py --max-questions-per-test 3
uv run scrape_wrong_questions.py --overwrite-existing
uv run scrape_wrong_questions.py --fresh
uv run scrape_wrong_questions.py --force-login-prompt
uv run scrape_wrong_questions.py --headless
```

Rebuild reports from an existing JSON file plus saved HTML snapshots without opening the browser:

```bash
uv run scrape_wrong_questions.py --rebuild-from-json outputs/wrong_questions.json
```

## Notes

- The scraper runs headed by default because College Board login is often interactive.
- Progress is saved after each question, so interrupted runs can be resumed.
- If you delete `playwright_profile/`, the next run will recreate it and require a fresh login.
- `wrong_questions.llm.md` is the best file to hand to an LLM for pattern analysis.
- Standalone HTML export is skipped automatically if `pandoc` is not installed.
