# AGENTS.md

Hard rules for AI-assisted edits in this repository. These are blocking, not advisory.

## Change scope

- Prefer the smallest correct change. Do not add abstractions, configuration, compatibility layers, fallbacks, or generalization unless a current requirement demands it.
- Prefer simple, flat control flow and guard clauses over deep nesting.
- Preserve current behavior unless the task explicitly changes it. Do not bulk-refactor unrelated code to make metrics prettier.
- Review the final diff and delete anything that can simply be deleted.

## Comments and docstrings

- Comments/docstrings must add information not recoverable from the code: rationale, invariants, external constraints, safety assumptions, or genuinely non-obvious behavior.
- Never add comments that narrate the next line, restate identifiers, record the coding session, explain the patch, or preserve stale implementation history.
- Skip the aislop `aislop:begin`/`aislop:end` blocks entirely — aislop manages those hook instructions itself.

## Tests

- Tests protect observable behavior, regressions, invariants, boundaries, state transitions, failure behavior, concurrency/order/idempotency, public contracts, or risky integrations.
- Do not add tests for framework behavior, trivial getters/wiring, cosmetic implementation details, or assertions that merely echo mocks/configuration.
- Do not weaken linting, tests, or quality gates to make a change pass.

## Complexity

- Never extract helpers solely to satisfy a metric if the result is harder to follow; simplify the control flow first.
- Ordinary application/business-logic functions stay within Ruff's C901 gate (max complexity 10; prefer <=8 where the design allows). Legacy exceptions are documented as debt in `pyproject.toml`; do not extend them.

## Quality gates

Existing CI must stay green. Run these before finishing (they are authoritative):

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check --extra-search-path . .
uv run pytest -q
uv run python scripts/aislop_changed_gate.py "$(git merge-base origin/main HEAD)"
```

## Final review checklist

Before submitting, answer each:

- What can be deleted?
- What abstraction/config/fallback was added without a current requirement?
- Can nested control flow become guards/early returns?
- Are comments explaining code that should instead be clearer?
- Does each new/changed test protect meaningful behavior?
- Did the implementation create multiple sources of truth?

No findings is a valid result. Do not manufacture findings to justify a review round.
