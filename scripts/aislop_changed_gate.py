#!/usr/bin/env python3
"""Fail aislop only for diagnostics attached to added lines."""

from __future__ import annotations

import json
import re
import subprocess
import sys

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def added_lines(  # noqa: C901 (diff parser state machine)
    base: str,
) -> tuple[dict[str, set[int]], set[str]]:
    result = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--"],
        check=True,
        capture_output=True,
        text=True,
    )
    ranges: dict[str, set[int]] = {}
    new_files: set[str] = set()
    path: str | None = None
    current: set[int] | None = None
    line = 0
    is_new = False
    for raw in result.stdout.splitlines():
        if raw.startswith("diff --git "):
            path = None
            current = None
            line = 0
            is_new = False
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            current = ranges.setdefault(path, set())
            if is_new:
                new_files.add(path)
            line = 0
            continue
        if raw.startswith("new file mode "):
            is_new = True
            continue
        match = HUNK.match(raw)
        if match:
            line = int(match.group(1))
            count = int(match.group(2) or "1")
            current = ranges.setdefault(path or "", set())
            current.update(range(line, line + count))
            line += count
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if current is not None:
                current.add(line)
            line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw and not raw.startswith("\\") and line:
            line += 1
    return ranges, new_files


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aislop_changed_gate.py <merge-base>")
    base = sys.argv[1]
    paths, new_files = added_lines(base)
    scan = subprocess.run(
        [
            "npx",
            "--yes",
            "aislop@0.16.0",
            "scan",
            "--changes",
            "--base",
            base,
            "--format",
            "json",
            ".",
        ],
        capture_output=True,
        text=True,
    )
    try:
        report = json.loads(scan.stdout)
    except json.JSONDecodeError as exc:
        print(scan.stdout, file=sys.stderr)
        raise SystemExit(f"aislop returned invalid JSON: {exc}") from exc
    diagnostics = []
    for finding in report.get("diagnostics", []):
        file_path = finding.get("filePath", "")
        line = int(finding.get("line") or 0)
        if file_path in new_files or line in paths.get(file_path, set()):
            diagnostics.append(finding)
    errors = [finding for finding in diagnostics if finding.get("severity") == "error"]
    print(json.dumps({"base": base, "diagnostics": diagnostics, "errors": len(errors)}, indent=2))
    if errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
