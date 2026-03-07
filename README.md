# SAT Bluebook Wrong-Question Scraper

This repo contains a local Playwright scraper that opens College Board My Practice in a persistent browser profile, lets you log in manually if needed, and exports every incorrect SAT Bluebook question it can collect.

## Files

- `scrape_wrong_questions.py`: single-file Python scraper runnable with `uv`
- `outputs/wrong_questions.json`
- `outputs/wrong_questions.csv`
- `outputs/wrong_questions.md`
- `outputs/drill_pack.md`
- `artifacts/`: screenshots, HTML snapshots, and image captures

## What it does

1. Launches Chromium with a persistent profile in `playwright_profile/`
2. Waits for you to log in manually if your session is not already valid
3. Finds SAT Practice Tests on the My Practice page
4. Opens each test, goes to `Score Details`, switches `View` to `All`, and keeps only rows marked `Incorrect`
5. Opens each `Review` page, turns on `Show correct answer and explanation`, scrapes what it can, saves artifacts, and writes outputs incrementally

## Exact commands

Install the Playwright browser once:

```bash
uv run --with playwright python -m playwright install chromium
```

Run the scraper:

```bash
uv run scrape_wrong_questions.py
```

Useful optional flags:

```bash
uv run scrape_wrong_questions.py --slow-mo 200 --max-tests 2
uv run scrape_wrong_questions.py --overwrite-existing
uv run scrape_wrong_questions.py --force-login-prompt
```

## Notes

- Keep the browser window visible; the scraper runs in headed mode by default.
- The script saves progress after each question, so reruns can continue from prior output.
- If the site layout shifts, the scraper tries multiple selectors and stores failure screenshots under `artifacts/errors/`.
