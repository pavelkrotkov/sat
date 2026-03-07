# Scrape incorrect SAT Bluebook questions from My Practice

Build a local scraper with Playwright that runs on my machine and extracts all SAT Bluebook practice-test questions I got wrong from College Board My Practice.

Use a persistent browser profile so my login/session survives reruns.

Do not ask me for credentials. Open the browser in headed mode and let me log in manually if needed.

Prefer Python. If you use Python, make it runnable with `uv` and put dependencies in the script header.

## What the agent should do

After login, the section **SAT Practice Tests** shows all past tests.

For each test:

1. Open the test.
2. Click **Score Details**.
3. In the page that opens, in the section **Questions Overview**, select **View: All**.
4. In the table below, keep only rows where **Your answer** contains **Incorrect**.
5. For each incorrect row, click **Review**.
6. In the question review screen, make sure **Show correct answer and explanation** is selected before scraping anything.
7. Scrape the question content, including text and pictures.
8. If pictures cannot be saved cleanly, take a screenshot.
9. Return back, or first collect all incorrect review links if that is easier, and continue until the whole test is scraped.
10. Return and continue to the next test.

## Output

Create:

- `outputs/wrong_questions.json`
- `outputs/wrong_questions.csv`
- `outputs/wrong_questions.md`
- `outputs/drill_pack.md`

Also save screenshots or other artifacts as needed under `artifacts/`.

## Required fields per wrong question

Capture whatever is available, especially:

- test name / test number
- section
- module if visible
- question number
- my answer
- correct answer
- question text
- answer choices
- explanation
- any images associated with the question
- screenshot path if used

## Drill pack

Make `outputs/drill_pack.md` useful for LLM study.

Group wrong questions by:
- Math vs Reading and Writing
- domain / skill if visible
- repeated patterns if inferable

For each group, include:
- the affected tests/questions
- a short weakness summary
- a shortlist of what to review first
- a few ready-to-paste prompts for Gemini / ChatGPT / Claude to drill that weakness

## Implementation notes

- This should run locally, not remotely.
- Use Playwright.
- Use a persistent browser profile.
- Save progress incrementally.
- Be reasonably robust to UI changes.
- If the site structure differs a bit, adapt instead of stopping immediately.
- If an item fails, save a screenshot and continue when possible.

## Deliverables

Produce:
1. the actual script
2. a short `README.md`
3. exact commands to run it

## Python requirement

If Python is used, make it a single script runnable with `uv`, with dependencies declared in the header.
