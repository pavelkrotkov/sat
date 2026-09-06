from pathlib import Path

from scripts.check_public_repo import looks_like_question_dump, private_reason, tracked_files, violations


def test_private_paths_and_exports_are_rejected():
    assert private_reason(Path("data/satprep.db"))
    assert private_reason(Path("cram_gemini/practice_questions.pdf"))
    assert private_reason(Path("notes/wrong_questions.json"))


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


def test_current_tracked_tree_stays_inside_public_boundary():
    assert violations(tracked_files()) == []
