from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_TRUSTED_MAX_COMPLEXITY = 10


@dataclass
class _DiffState:
    path: str | None = None
    current: set[int] | None = None
    line: int = 0
    is_new: bool = False


def _consume_diff_line(
    raw: str,
    state: _DiffState,
    ranges: dict[str, set[int]],
    new_files: set[str],
) -> None:
    if raw.startswith("diff --git "):
        state.path = None
        state.current = None
        state.line = 0
        state.is_new = False
        return
    if raw.startswith("new file mode "):
        state.is_new = True
        return
    if raw.startswith("+++ b/"):
        state.path = raw[6:]
        state.current = ranges.setdefault(state.path, set())
        if state.is_new:
            new_files.add(state.path)
        return
    match = _HUNK.match(raw)
    if match:
        state.line = int(match.group(1))
        return
    if raw.startswith("+") and not raw.startswith("+++"):
        if state.current is not None:
            state.current.add(state.line)
        state.line += 1
        return
    if raw.startswith("-") and not raw.startswith("---"):
        return
    if raw and not raw.startswith("\\") and state.line:
        state.line += 1


def added_lines(base: str) -> tuple[dict[str, set[int]], set[str]]:
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--"],
        check=True,
        capture_output=True,
        text=True,
    )
    ranges: dict[str, set[int]] = {}
    new_files: set[str] = set()
    state = _DiffState()
    for raw in result.stdout.splitlines():
        _consume_diff_line(raw, state, ranges, new_files)
    return ranges, new_files


def _ruff_c901(directory: str) -> list[dict]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            "C901",
            "--ignore-noqa",
            "--config",
            f"lint.mccabe.max-complexity={_TRUSTED_MAX_COMPLEXITY}",
            "--output-format=json",
            ".",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"ruff C901 check failed (code {result.returncode})")
    try:
        report = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"ruff returned invalid JSON: {exc}") from exc
    if not isinstance(report, list) or not all(isinstance(item, dict) for item in report):
        raise SystemExit("ruff returned an invalid C901 report")
    return report


def _function_spans(root: str, relpath: str) -> list[tuple[int, int]]:
    path = Path(root) / relpath
    if path.suffix != ".py":
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def _relative_filename(directory: str, filename: str) -> str:
    path = Path(filename)
    if not path.is_absolute():
        path = Path(directory) / path
    try:
        return path.resolve().relative_to(Path(directory).resolve()).as_posix()
    except ValueError:
        return filename.replace("\\", "/")


def _c901_touches_added_lines(
    diagnostic: dict,
    directory: str,
    paths: dict[str, set[int]],
    new_files: set[str],
) -> bool:
    relpath = _relative_filename(directory, diagnostic.get("filename", ""))
    if relpath in new_files:
        return True
    added = paths.get(relpath, set())
    location = diagnostic.get("location")
    row = location.get("row") if isinstance(location, dict) else None
    if not isinstance(row, int) or not added:
        return False
    if row in added:
        return True
    return any(
        start <= row <= end and any(start <= line <= end for line in added)
        for start, end in _function_spans(directory, relpath)
    )


def changed_c901(
    directory: str,
    paths: dict[str, set[int]],
    new_files: set[str],
) -> list[dict]:
    return [
        diagnostic
        for diagnostic in _ruff_c901(directory)
        if _c901_touches_added_lines(diagnostic, directory, paths, new_files)
    ]


def _diagnostic_row(diagnostic: dict) -> int | None:
    location = diagnostic.get("location")
    return location.get("row") if isinstance(location, dict) else None


def c901_blocking_diagnostics(
    directory: str,
    paths: dict[str, set[int]],
    new_files: set[str],
) -> list[dict]:
    return [
        {
            "filePath": _relative_filename(directory, diagnostic.get("filename", "")),
            "rule": "C901",
            "severity": "error",
            "line": _diagnostic_row(diagnostic),
            "detail": diagnostic.get("message"),
            "message": "changed code exceeds Ruff's complexity limit",
        }
        for diagnostic in changed_c901(directory, paths, new_files)
    ]
