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


def tracked_files() -> list[pathlib.Path]:
    raw = subprocess.check_output(("git", "ls-files", "-z"))
    return [pathlib.Path(name) for name in raw.decode().split("\0") if name]


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
    text = path.read_text(encoding="utf-8", errors="ignore")
    lower = text.lower()
    choices = set(CHOICE.findall(text))
    has_answer = any(marker in lower for marker in ("correct answer", "answer key", '"correct_answer"', '"correct_letter"'))
    has_question = "question" in lower and any(marker in lower for marker in ("passage", "stimulus", "rationale", "explanation"))
    return choices == set("ABCD") and has_answer and has_question


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
    found = violations(tracked_files())
    if found:
        print("Public repository leak guard failed:", file=sys.stderr)
        print("\n".join(f"  - {item}" for item in found), file=sys.stderr)
        return 1
    print("Public repository leak guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
