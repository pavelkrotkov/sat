#!/usr/bin/env python3
"""Fail when tracked files cross the repository's public/private data boundary."""
from __future__ import annotations
import pathlib, re, subprocess, sys

PRIVATE_PREFIXES = ("data/", "imports/", "outputs/", "artifacts/", "exports/", "backups/", "cram_claude/", "cram_gemini/", "kb/", "playwright_profile/")
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pdf", ".xlsx", ".csv"}
PRIVATE_NAME = re.compile(r"(?:wrong_questions|drill_pack|practice_(?:questions|answers)|storage_state|cookies)(?:\.|$)", re.IGNORECASE)
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".html"}
CHOICE = re.compile(r"(?m)^\s*(?:[-*]\s*)?([A-D])[.)\]:]\s+\S")
JSON_CHOICE = re.compile(r'"letter"\s*:\s*"([A-D])"')
ANSWER_MARKERS = ("correct answer", "answer key", '"correct_answer"', '"correct_letter"')
QUESTION_MARKERS = ("passage", "stimulus", "rationale", "explanation")

class ScanError(RuntimeError): pass

def tracked_files() -> list[pathlib.Path]:
    try:
        raw = subprocess.check_output(("git", "ls-files", "-z"), stderr=subprocess.PIPE)
        names = raw.decode().split("\0")
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise ScanError(f"cannot enumerate tracked files: {error}") from error
    return [pathlib.Path(name) for name in names if name]

def private_reason(path: pathlib.Path) -> str | None:
    name, suffix = path.as_posix(), path.suffix.lower()
    rules = (
        (name.startswith(PRIVATE_PREFIXES), "private/runtime path"),
        (suffix in PRIVATE_SUFFIXES or suffix.startswith((".sqlite", ".db-")), "private/export file type"),
        (path.name.startswith(".env") and path.name != ".env.example", "secret-bearing environment file"),
        (bool(PRIVATE_NAME.search(path.name)) and suffix not in {".py", ".sh"}, "known question/export artifact name"),
    )
    return next((reason for blocked, reason in rules if blocked), None)

def _has(lower: str, markers: tuple[str, ...]) -> bool:
    return any(marker in lower for marker in markers)

def _rendered_shape(lower: str) -> bool:
    return "question" in lower and _has(lower, QUESTION_MARKERS)

def _structured_shape(lower: str) -> bool:
    quoted = tuple(f'"{marker}"' for marker in QUESTION_MARKERS)
    return '"choices"' in lower and '"stem"' in lower and _has(lower, quoted)

def looks_like_question_dump(path: pathlib.Path) -> bool:
    if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        raise ScanError(f"cannot read tracked text {path}: {error}") from error
    lower = text.lower()
    choices = set(CHOICE.findall(text)) | set(JSON_CHOICE.findall(text))
    shape = _rendered_shape(lower) or _structured_shape(lower)
    return choices == set("ABCD") and _has(lower, ANSWER_MARKERS) and shape

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
        message = "\n".join(f"  - {item}" for item in found)
        print(f"Public repository leak guard failed:\n{message}", file=sys.stderr)
        return 1
    print("Public repository leak guard passed.")
    return 0

if __name__ == "__main__": raise SystemExit(main())
