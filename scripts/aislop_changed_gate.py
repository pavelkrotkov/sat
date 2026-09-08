#!/usr/bin/env python3
"""Fail aislop only on slop that a diff newly introduces.

Compares aislop findings on the HEAD working tree against the same files at
the PR merge-base, so pre-existing ("legacy") findings never fail the gate
while new or worsened violations do. Function-level signatures include the
reported metric and use aislop's changed-span context, so changed bodies are
checked even when the diagnostic remains anchored at an unchanged `def` line.
Line-level findings are keyed by affected source text, so line-number shifts do
not turn unchanged legacy findings into false positives.

Blocking = a new finding that is error-severity (the .aislop/config.yml rules
elevated to `error`) or a quality/score-threshold violation that the diff
introduces:
  - ai-slop/* -> blocked (config elevates these to error)
  - complexity/function-too-long, complexity/file-too-large,
    complexity/deep-nesting, complexity/too-many-params -> blocked
    (config `quality.*` thresholds)
Exit 1 when any such finding is new relative to the base.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
AISLOP = ["npx", "--yes", "aislop@0.16.0", "scan", "--format", "json", "."]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# Quality/score-threshold rules whose NEW appearance must block even though they
# are warning severity in aislop output. These are the config `quality.*`
# thresholds (maxFunctionLoc / maxFileLoc / maxNesting / maxParams) plus the
# ai-slop categories the repo config elevates to error.
BLOCKING_RULES = (
    "complexity/function-too-long",
    "complexity/file-too-large",
    "complexity/deep-nesting",
    "complexity/too-many-params",
)

_FUNC_NAME = re.compile(r"^(.+?) · (?:\d+|depth \d+)")


def run_aislop(directory: str, base: str | None = None) -> dict:
    """Run `aislop scan` against ``directory`` and return the JSON report."""
    command = AISLOP
    if base is not None:
        command = [*AISLOP[:-1], "--changes", "--base", base, AISLOP[-1]]
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"aislop returned invalid JSON: {exc}") from exc


def changed_files(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [p for p in result.stdout.splitlines() if p]


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
        if raw.startswith("new file mode "):
            is_new = True
            continue
        if raw.startswith("+++ b/"):
            path = raw[6:]
            current = ranges.setdefault(path, set())
            if is_new:
                new_files.add(path)
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


def materialize_base(base: str, files: list[str], config_files: list[str], directory: str) -> None:
    """Write base versions of `files` and `.aislop` config to a temp dir."""
    for rel in files + config_files:
        blob = subprocess.run(
            ["git", "show", f"{base}:{rel}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if blob.returncode != 0:  # file absent at base (newly added) -> no baseline
            continue
        target = Path(directory) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(blob.stdout, encoding="utf-8")


def file_line_text(root: str, relpath: str, line: int) -> str:
    """Return the source line text at `relpath:line`, or '' if unreadable."""
    if line < 1:
        return ""
    try:
        lines = (Path(root) / relpath).read_text(encoding="utf-8").splitlines()
        return lines[line - 1] if line <= len(lines) else ""
    except OSError:
        return ""


def finding_signature(finding: dict, root: str) -> tuple:
    """Key a finding so base vs head comparisons ignore line-number drift.

    Function-level findings include the measured value from `detail`, so a
    threshold violation that worsens has a new signature. Line-level findings
    use source text, which stays stable when code merely shifts lines.
    """
    relpath = finding.get("filePath", "")
    rule = finding.get("rule", "")
    func_match = _FUNC_NAME.match(finding.get("detail", ""))
    if func_match:
        return ("func", relpath, rule, func_match.group(1), finding.get("detail", ""))
    text = file_line_text(root, relpath, int(finding.get("line") or 0))
    return ("line", relpath, rule, text)


def is_blocking(finding: dict) -> bool:
    severity = finding.get("severity")
    return severity == "error" or finding.get("rule", "") in BLOCKING_RULES


def is_changed_finding(
    finding: dict,
    paths: dict[str, set[int]],
    new_files: set[str],
    *,
    is_new: bool,
) -> bool:
    file_path = finding.get("filePath", "")
    line = int(finding.get("line") or 0)
    if file_path in new_files:
        return True
    line_changed = line in paths.get(file_path, set())
    if _FUNC_NAME.match(finding.get("detail", "")):
        return is_new and (finding.get("changeContext") == "changed-line" or line_changed)
    return is_new or line_changed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aislop_changed_gate.py <merge-base>")
    base = sys.argv[1]

    files = changed_files(base)
    paths, new_files = added_lines(base)
    config_files = [".aislop/config.yml", ".aislop/rules.yml"]

    head = run_aislop(str(_ROOT), base)
    head_diags = [d for d in head.get("diagnostics", []) if d.get("filePath") in files]
    head_sigs = {finding_signature(d, str(_ROOT)) for d in head_diags}
    with tempfile.TemporaryDirectory(prefix="aislop_base_") as base_dir:
        materialize_base(base, files, config_files, base_dir)
        base_report = run_aislop(base_dir)
        base_diags = [d for d in base_report.get("diagnostics", []) if d.get("filePath") in files]
        base_sigs = {finding_signature(d, base_dir) for d in base_diags}
    new_sigs = head_sigs - base_sigs
    new = [
        d
        for d in head_diags
        if is_changed_finding(
            d,
            paths,
            new_files,
            is_new=finding_signature(d, str(_ROOT)) in new_sigs,
        )
    ]
    blocking = [d for d in new if is_blocking(d)]

    summary = {
        "base": base,
        "changed_files": len(files),
        "head_diagnostics": len(head_diags),
        "base_diagnostics": len(base_diags),
        "new_diagnostics": len(new),
        "blocking_diagnostics": len(blocking),
        "blocking": [
            {
                "file": d.get("filePath"),
                "rule": d.get("rule"),
                "severity": d.get("severity"),
                "line": d.get("line"),
                "detail": d.get("detail"),
                "message": d.get("message"),
            }
            for d in blocking
        ],
    }
    print(json.dumps(summary, indent=2))
    if blocking:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
