from pathlib import Path

from satprep.parse_snapshot import parse_snapshot

SNAP_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "html"


def test_parses_incorrect_review_snapshot():
    f = SNAP_DIR / "sat-practice-test-7-reading-and-writing-module-2-7-7-reading-and-writing-d-b-inc.html"
    p = parse_snapshot(f.read_text())
    assert p.section.startswith("Reading and Writing")
    assert p.question_number == "7"
    assert len(p.choices) == 4
    assert [c["letter"] for c in p.choices] == list("ABCD")
    assert p.correct_letter == "D"
    assert p.student_letter == "B"
    assert "Magic Mountain" in p.passage or "Hans Castorp" in p.passage
    assert p.stem.startswith("What does the text most strongly suggest")
    assert p.rationale.lower().startswith("choice d")


def test_parses_correct_review_snapshot_without_choices():
    f = SNAP_DIR / "sat-practice-test-4-reading-and-writing-module-1-1-1-reading-and-writing-correct.html"
    p = parse_snapshot(f.read_text())
    assert p.question_number == "1"
    assert p.student_letter == "B"
    assert p.correct_letter == "B"
    assert p.choices == []          # bluebook omits options on correct reviews
    assert p.stem                  # stem must still be recovered


def test_snapshot_parse_is_deterministic():
    files = sorted(SNAP_DIR.glob("*reading-and-writing*.html"))[:20]
    for f in files:
        assert parse_snapshot(f.read_text()).__dict__ == parse_snapshot(f.read_text()).__dict__
