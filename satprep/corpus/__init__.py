"""Question content: ingestion, fingerprints, tags, pools, archive.

Rebuildable and disposable. Everything here can be reconstructed from the
raw sources under outputs/ and artifacts/, or from exports/corpus-v1.jsonl
alone - which is exactly why it is separated from `satprep.training`, whose
attempt history has no such second copy.

This package must never import from `satprep.training`. The dependency runs
one way: training reads the corpus, the corpus knows nothing about drills.
Enforced by tests/test_module_boundaries.py.
"""
