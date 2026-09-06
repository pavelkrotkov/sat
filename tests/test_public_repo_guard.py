from pathlib import Path

import pytest

from scripts.check_public_repo import (
    ScanError,
    looks_like_question_dump,
    private_reason,
    tracked_files,
    violations,
)


def test_private_paths_and_exports_are_rejected():
    assert private_reason(Path("data/satprep.db"))
    assert private_reason(Path("cram_gemini/practice_questions.pdf"))
    assert private_reason(Path("notes/wrong_questions.json"))
    assert private_reason(Path("notes/satprep.db.gz"))


def test_full_question_dump_is_rejected_but_normal_docs_are_not(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dump = Path("dump.md")
    dump.write_text(
        "Question\nPassage: fabricated\nA) one\nB) two\nC) three\nD) four\nCorrect answer: A\nExplanation: fabricated\n"
    )
    safe = Path("README.md")
    safe.write_text("This project may refer descriptively to SAT and College Board.\n")
    assert looks_like_question_dump(dump)
    assert not looks_like_question_dump(safe)


def test_native_json_question_records_are_rejected(tmp_path):
    archive = tmp_path / "corpus.jsonl"
    archive.write_text(
        '{"stem":"Fabricated?","passage":"Synthetic","choices":['
        '{"letter":"A","text":"one"},{"letter":"B","text":"two"},'
        '{"letter":"C","text":"three"},{"letter":"D","text":"four"}],'
        '"correct_letter":"A","rationale":"Synthetic"}\n'
    )
    scrape = tmp_path / "renamed.json"
    scrape.write_text(
        '{"question_text":"Fabricated?","answer_choices":['
        '"A. one","B. two","C. three","D. four"],'
        '"correct_answer":"A","explanation":"Synthetic"}\n'
    )
    assert looks_like_question_dump(archive)
    assert looks_like_question_dump(scrape)


def test_scan_errors_fail_closed(tmp_path, monkeypatch):
    text = tmp_path / "tracked.md"
    text.write_text("safe")

    def unreadable(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ScanError):
        looks_like_question_dump(text)


def test_git_enumeration_errors_fail_closed(monkeypatch):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("scripts.check_public_repo.subprocess.check_output", missing_git)
    with pytest.raises(ScanError):
        tracked_files()


def test_current_tracked_tree_stays_inside_public_boundary():
    assert violations(tracked_files()) == []
