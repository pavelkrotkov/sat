#!/usr/bin/env python3
"""Fail when tracked files cross the repository's public/private data boundary."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

PRIVATE_PREFIXES = (
    "data/",
    "imports/",
    "outputs/",
    "artifacts/",
    "exports/",
    "backups/",
    "cram_claude/",
    "cram_gemini/",
    "kb/",
    "playwright_profile/",
)
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pdf", ".xlsx", ".csv"}
PRIVATE_NAME = re.compile(
    r"(?:wrong_questions|drill_pack|practice_(?:questions|answers)|storage_state|cookies)(?:\.|$)",
    re.IGNORECASE,
)
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".html"}
CHOICE = re.compile(r"(?m)^\s*(?:[-*]\s*)?([A-D])[.)\]:]\s+\S")
JSON_CHOICE = re.compile(r'"letter"\s*:\s*"([A-D])"')
ANSWER_MARKERS = ("correct answer", "answer key", '"correct_answer"', '"correct_letter"')
QUESTION_MARKERS = ("passage", "stimulus", "rationale", "explanation")


class ScanError(RuntimeError):
    pass


def tracked_files() -> list[pathlib.Path]:
    try:
        raw = subprocess.check_output(("git", "ls-files", "-z"), stderr=subprocess.PIPE)
        names = raw.decode().split("\0")
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ScanError(f"cannot enumerate tracked files: {error}") from error
    return [pathlib.Path(name) for name in names if name]


def private_reason(path: pathlib.Path) -> str | None:
    name = path.as_posix()
    suffix = path.suffix.lower()
    if name.startswith(PRIVATE_PREFIXES):
        return "private/runtime path"
    if suffix in PRIVATE_SUFFIXES or suffix.startswith(".sqlite") or suffix.startswith(".db-"):
        return "private/export file type"
    if path.name.startswith(".env") and path.name != ".env.example":
        return "secret-bearing environment file"
    if PRIVATE_NAME.search(path.name) and suffix not in {".py", ".sh"}:
        return "known question/export artifact name"
    return None


def looks_like_question_dump(path: pathlib.Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        raise ScanError(f"cannot read tracked text {path}: {error}") from error
    lower = text.lower()
    choices = set(CHOICE.findall(text)) | set(JSON_CHOICE.findall(text))
    has_answer = any(marker in lower for marker in ANSWER_MARKERS)
    rendered = "question" in lower and any(marker in lower for marker in QUESTION_MARKERS)
    structured = '"choices"' in lower and '"stem"' in lower and any(
        f'"{marker}"' in lower for marker in QUESTION_MARKERS
    )
    return choices == set("ABCD") and has_answer and (rendered or structured)


def violations(paths: list[pathlib.Path]) -> list[str]:
    found = []
    for path in paths:
        reason = private_reason(path)
        if reason:
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
        print("Public repository leak guard failed:", file=sys.stderr)
        print("\n".join(f"  - {item}" for item in found), file=sys.stderr)
        return 1
    print("Public repository leak guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
