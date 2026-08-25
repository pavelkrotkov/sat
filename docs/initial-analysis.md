# Initial Analysis Report

_Computed from the live corpus on 2026-08-25 07:56 by `satprep` (weakness sampler-v1). Regenerate with `uv run satprep stats`._

## Corpus
- 2280 questions total: 472 historical R&W, 1363 fresh training, **445 protected benchmark (unseen)**
- Historical display limitation: Bluebook omits choices on correctly-answered reviews; 425 historical items are statistics-only and never displayed.

## Top official-skill weaknesses (model score · wrong/seen)
- Inferences: **44.1** (16w/40)
- Form, Structure, and Sense: **32.6** (6w/6)
- Boundaries: **27.6** (3w/3)
- Words in Context: **26.8** (18w/78)
- Cross-Text Connections: **26.4** (2w/7)
- Command of Evidence: **25.7** (6w/27)

## Top reasoning-pattern weaknesses (risk · wrong/seen · shaky-correct)
- `near_synonym_distinction`: **48.2** (17w/19, 2 shaky-correct)
- `hypothesis_vs_result`: **44.9** (12w/17, 5 shaky-correct)
- `qualifier_strength`: **36.6** (7w/7, 0 shaky-correct)
- `paraphrase_precision`: **34.9** (14w/36, 22 shaky-correct)
- `scientific_noun_overload`: **32.4** (40w/139, 99 shaky-correct)
- `abstract_relationship_extraction`: **30.8** (10w/32, 22 shaky-correct)
- `word_sense_in_context`: **30.8** (4w/6, 2 shaky-correct)
- `cause_vs_correlation`: **29.4** (4w/4, 0 shaky-correct)

## Transfer material available in weak categories
- `near_synonym_distinction`: 2 historical-correct of 19 seen; 398 fresh bank matches (147 hard)
- `hypothesis_vs_result`: 5 historical-correct of 17 seen; 30 fresh bank matches (13 hard)
- `qualifier_strength`: 0 historical-correct of 7 seen; 140 fresh bank matches (71 hard)
- `paraphrase_precision`: 22 historical-correct of 36 seen; 433 fresh bank matches (161 hard)
- `scientific_noun_overload`: 98 historical-correct of 138 seen; 285 fresh bank matches (125 hard)

## Diagnosed misconception patterns (rule-based, from stored wrong letters)
- `same_topic_wrong_relationship` ×16
- `contrast_concession` ×4
- `cause_vs_correlation` ×2
- 73 misses left undiagnosed — insufficient data (no stored wrong letter or choice text); not guessed.

## Recommended initial drill mix
- Targeted Drill composition achieved (seed `initial-report`): {'best_available': 3, 'old_wrong_due': 4, 'fresh_weak': 5}
- Focus tags: `hypothesis_vs_result`, `near_synonym_distinction`, `qualifier_strength`; skill focus: Inferences.
- Take a Fresh Benchmark first while all protected items are unseen for an honest baseline.
