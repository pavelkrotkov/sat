# Public/private data boundary

The public repository is code-only. A file belongs in Git only when it is
needed to build, test, document, or generically deploy the software without
carrying a real student's data or redistributed question content.

## Public

- `satprep/` application source
- synthetic/fabricated tests
- reusable scripts and generic deployment templates
- GitHub workflows and development configuration
- public documentation, package metadata, lockfile, and license

## Local/private

Never commit:

- SAT/Bluebook/Question Bank question text, choices, answer keys, rationales, or
  snapshots copied from a real source
- SQLite databases, attempts, scores, history, weakness state, reviews, and
  generated coaching/report output
- imports, exports, corpus archives, backups, generated practice sets, and
  scraped HTML/images
- browser profiles, cookies, session/auth state, local secrets, or credentials
- private/raw knowledge-base material or transcripts unless separately cleared
  for redistribution

The expected local directories are `data/`, `imports/`, `outputs/`,
`artifacts/`, `exports/`, `backups/`, `kb/`, and `playwright_profile/`. They are
ignored by Git and rejected if tracked by `scripts/check_public_repo.py`.

Synthetic test records are allowed in Python tests. They should use obviously
fabricated values rather than copied question prose.

## Before any visibility change

Removing files in a normal commit does not remove them from old commits. This
repository must stay private until its historical and GitHub-hosted surfaces are
proven clean.

Use a fresh mirror/clone to inventory **all** historical paths and refs, then
rewrite or replace the public history. At minimum inspect:

```bash
git log --all --name-only --pretty=format: | sort -u
for ref in $(git for-each-ref --format='%(refname)' refs/heads refs/tags); do
  git ls-tree -r --name-only "$ref"
done
```

Known historical private paths include `cram_claude/`, `cram_gemini/`, `kb/`,
`docs/initial-analysis.md`, and generated/runtime data patterns enforced by the
leak guard. Do not assume this list is exhaustive; derive the final removal set
from the inventory.

For an in-place rewrite, `git filter-repo` is preferred. Because old README,
deployment, branch, and PR history may also contain machine-specific or private
context, a new clean root/repository made from the verified code-only tree is
safer than preserving history when there is any doubt.

After rewriting, run both a dedicated secret scanner and the repository guard
across every intended public ref, for example:

```bash
gitleaks git .
python scripts/check_public_repo.py
git grep -n -I -E '(/Users/[^/]+|/home/[^/]+|wrong_questions|SAT Practice Test|Bluebook|My Practice)' $(git rev-list --all)
```

Review matches rather than treating generic descriptive references as leaks.
The target is zero credentials, personal-machine paths, copied question
records, student data, or private artifacts.

Also audit GitHub surfaces that a Git rewrite does not change: issue and PR
bodies/comments/reviews, attachments, Actions logs/artifacts, and releases.
If any GitHub-hosted object cannot be reliably sanitized, publish a separate
clean repository instead of making this archival repository public.

Finally verify from a fresh unauthenticated clone that only intended code and
docs are reachable. Repository visibility is a manual owner decision after
that verification; CI or an implementation PR must not change it automatically.
