#!/usr/bin/env python3
"""Fail when tracked files cross the repository's public/private data boundary."""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

# Block ownership classes rather than enumerating generated filenames one by one.
# `.env.example` and source files whose names mention exports remain allowed.
PRIVATE = re.compile(
    r"^(?:data|imports|outputs|artifacts|exports|backups|cram_claude|cram_gemini|kb|playwright_profile)/"
    r"|(?:^|/)\.env(?!\.example$)|\.(?:db(?:-|$)|sqlite3?(?:-|$)|pdf$|xlsx$|csv$)"
    r"|(?:wrong_questions|drill_pack|practice_(?:questions|answers)|storage_state|cookies)(?:\.(?!py$|sh$)|$)",
    re.IGNORECASE,
)
TEXT = {".md", ".txt", ".json", ".jsonl", ".html"}
# Content detection is conjunctive: four choices, an answer marker, and question shape.
# Both rendered A-D choices and the application's native JSON choice shape are recognized.
# Ordinary SAT/College Board documentation therefore remains a safe false-positive boundary.
# The same record-shape rule is shared by local pre-commit and CI.
CHOICE = re.compile(
    r'(?m)(?:^\s*(?:[-*]\s*)?|"letter"\s*:\s*")([A-D])(?:[.)\]:]\s+\S|")'
)
ANSWER = re.compile(r'correct answer|answer key|"correct_(?:answer|letter)"', re.IGNORECASE)
SHAPE = re.compile(
    r'(?is)(?:(?=.*\bquestion\b)(?=.*(?:passage|stimulus|rationale|explanation))|'
    r'(?=.*"choices")(?=.*"stem")(?=.*"(?:passage|stimulus|rationale|explanation)"))'
)


class ScanError(RuntimeError):
    pass


def tracked_files() -> list[pathlib.Path]:
    try:
        raw = subprocess.check_output(("git", "ls-files", "-z"), stderr=subprocess.PIPE)
        return [pathlib.Path(path) for path in raw.decode().split("\0") if path]
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ScanError(f"cannot enumerate tracked files: {error}") from error


def private_reason(path: pathlib.Path) -> str | None:
    return "private/runtime artifact" if PRIVATE.search(path.as_posix()) else None


def looks_like_question_dump(path: pathlib.Path) -> bool:
    if path.suffix.lower() not in TEXT or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        raise ScanError(f"cannot read tracked text {path}: {error}") from error
    return (
        set(CHOICE.findall(text)) == set("ABCD")
        and bool(ANSWER.search(text))
        and bool(SHAPE.search(text))
    )


def violations(paths: list[pathlib.Path]) -> list[str]:
    found = []
    for path in paths:
        if reason := private_reason(path):
            found.append(f"{path}: {reason}")
        elif looks_like_question_dump(path):
            found.append(f"{path}: looks like a full question record/dump")
    return found


def main() -> int:
    try:
        found = violations(tracked_files())
    except ScanError as error:
        print(f"Public repository leak guard could not complete: {error}", file=sys.stderr)
        return 2
    if found:
        print("Public repository leak guard failed:\n  - " + "\n  - ".join(found), file=sys.stderr)
        return 1
    print("Public repository leak guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
