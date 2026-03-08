# SAT Bluebook Wrong-Question Scraper

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
- `pandoc-report.css`: CSS used for standalone HTML export

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
6. Checkpoints JSON after each question for crash recovery, then renders the full report set and standalone HTML at the end if `pandoc` is available

## Setup

Install the Playwright browser once:

```bash
uv run --with playwright python -m playwright install chromium
```

Optional but recommended for standalone HTML export:

```bash
brew install pandoc
```

Required for `export_md_to_pdf.sh`:

- Google Chrome or Chromium installed locally

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
uv run scrape_wrong_questions.py --save-page-visits --save-error-screenshots
uv run scrape_wrong_questions.py --save-question-screenshots
```

Rebuild reports from an existing JSON file plus saved HTML snapshots without opening the browser:

```bash
uv run scrape_wrong_questions.py --rebuild-from-json outputs/wrong_questions.json
```

Export any Markdown file to PDF through Pandoc HTML plus headless Chrome printing:

```bash
./export_md_to_pdf.sh outputs/wrong_questions.md
./export_md_to_pdf.sh cram_gemini/answer_tactics.md /tmp/answer_tactics.pdf
```

Notes for the PDF script:

- it uses [pandoc-report.css](/Users/pavel/dev/sat/pandoc-report.css) by default
- it applies a print-time `90%` zoom
- override the stylesheet with `CSS_PATH=/path/to/pandoc-report.css`
- override the browser binary with `CHROME_BIN=/path/to/chrome`

## Notes

- The scraper runs headed by default because College Board login is often interactive.
- Progress is checkpointed to `wrong_questions.json` after each question, so interrupted runs can be resumed without regenerating every report on the hot path.
- If a run is interrupted, use `--rebuild-from-json outputs/wrong_questions.json` to regenerate Markdown, drill-pack, and HTML outputs from the saved snapshots.
- `artifacts/html/` and `artifacts/images/` are the default artifact set because they support rebuilds and parser fixes.
- The heavier debug artifacts are opt-in via `--save-page-visits`, `--save-question-screenshots`, and `--save-error-screenshots`.
- If you delete `playwright_profile/`, the next run will recreate it and require a fresh login.
- `wrong_questions.llm.md` is the best file to hand to an LLM for pattern analysis.
- Standalone HTML export is skipped automatically if `pandoc` is not installed.
- `export_md_to_pdf.sh` depends on both `pandoc` and a local Chrome/Chromium binary.
