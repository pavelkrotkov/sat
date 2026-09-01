from pathlib import Path

import pytest

from satprep.corpus.parse_snapshot import parse_snapshot

SNAP_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "html"


# PR-43 review: these tests require gitignored HTML fixtures in
# artifacts/html/ that only exist on a developer's local checkout. The
# whole directory is too large (38M+) to commit, and CI must not be
# coupled to a developer's local scrape. Skip when the fixtures are
# missing rather than failing collection; the test is still
# meaningful for whoever has the fixtures.
_FIXTURE_INCORRECT = (
    SNAP_DIR
    / "sat-practice-test-7-reading-and-writing-module-2-7-7-reading-and-writing-d-b-inc.html"
)
_FIXTURE_CORRECT = (
    SNAP_DIR
    / "sat-practice-test-4-reading-and-writing-module-1-1-1-reading-and-writing-correct.html"
)


@pytest.mark.skipif(
    not _FIXTURE_INCORRECT.exists(),
    reason="parse_snapshot fixture not in working tree (artifacts/html/ is gitignored; see #36)",
)
def test_parses_incorrect_review_snapshot():
    p = parse_snapshot(_FIXTURE_INCORRECT.read_text())
    assert p.section.startswith("Reading and Writing")
    assert p.question_number == "7"
    assert len(p.choices) == 4
    assert [c["letter"] for c in p.choices] == list("ABCD")
    assert p.correct_letter == "D"
    assert p.student_letter == "B"
    assert "Magic Mountain" in p.passage or "Hans Castorp" in p.passage
    assert p.stem.startswith("What does the text most strongly suggest")
    assert p.rationale.lower().startswith("choice d")


@pytest.mark.skipif(
    not _FIXTURE_CORRECT.exists(),
    reason="parse_snapshot fixture not in working tree (artifacts/html/ is gitignored; see #36)",
)
def test_parses_correct_review_snapshot_without_choices():
    p = parse_snapshot(_FIXTURE_CORRECT.read_text())
    assert p.question_number == "1"
    assert p.student_letter == "B"
    assert p.correct_letter == "B"
    assert p.choices == []  # bluebook omits options on correct reviews
    assert p.stem  # stem must still be recovered


@pytest.mark.skipif(
    not SNAP_DIR.exists() or not any(SNAP_DIR.glob("*reading-and-writing*.html")),
    reason="parse_snapshot fixtures not in working tree",
)
def test_snapshot_parse_is_deterministic():
    files = sorted(SNAP_DIR.glob("*reading-and-writing*.html"))[:20]
    for f in files:
        assert parse_snapshot(f.read_text()).__dict__ == parse_snapshot(f.read_text()).__dict__
