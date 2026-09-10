#!/usr/bin/env python3
"""Fail aislop only on slop that a diff newly introduces.

Compares aislop findings on the HEAD working tree against the same files at
the PR merge-base, so pre-existing ("legacy") findings never fail the gate
while new or worsened violations do. Function-level signatures include the
reported metric and use aislop's changed-span context, so changed bodies are
checked even when the diagnostic remains anchored at an unchanged `def` line.
Line-level findings are keyed by source text when available; otherwise by the
`detail` field so metric-based warnings (like file-size growth) can still
change between base and head.

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

import yaml

_ROOT = Path(__file__).resolve().parent.parent
AISLOP = ["npx", "--yes", "aislop@0.16.0", "scan", "--format", "json", "."]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DEFAULT_CONFIG_WARNING = "using default configuration"

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
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"aislop returned invalid JSON: {exc}") from exc

    if result.returncode != 0 and not report:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f"aislop command failed (code {result.returncode})")

    if DEFAULT_CONFIG_WARNING in f"{result.stdout}{result.stderr}".lower():
        raise SystemExit("aislop fell back to default configuration")

    return report


def aislop_fail_below(config_path: Path) -> float | None:
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SystemExit(f"unable to read {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SystemExit(f"invalid yaml in {config_path}: {exc}") from exc

    ci = payload.get("ci") if isinstance(payload, dict) else None
    if not isinstance(ci, dict):
        return None
    fail_below = ci.get("failBelow")
    if fail_below is None:
        return None

    try:
        return float(fail_below)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid ci.failBelow in {config_path}: {fail_below!r}") from exc


def score_blocking(report: dict, config_path: Path) -> bool:
    fail_below = aislop_fail_below(config_path)
    if fail_below is None:
        return False

    score = report.get("score")
    if score is None:
        return False

    try:
        return float(score) < fail_below
    except (TypeError, ValueError):
        return False


def score_worsened_since_base(base_report: dict, head_report: dict, config_path: Path) -> bool:
    fail_below = aislop_fail_below(config_path)
    if fail_below is None:
        return False

    head_score = head_report.get("score")
    if head_score is None:
        return False

    try:
        head_score = float(head_score)
    except (TypeError, ValueError):
        return False

    if head_score >= fail_below:
        return False

    base_score = base_report.get("score")
    if base_score is None:
        raise SystemExit("base report missing score")

    try:
        base_score = float(base_score)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid score in base report: {base_score!r}") from exc

    if base_score >= fail_below and head_score < fail_below:
        return True
    return head_score < base_score


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
    line = int(finding.get("line") or 0)
    func_match = _FUNC_NAME.match(finding.get("detail", ""))
    if func_match and line > 0:
        return ("func", relpath, rule, func_match.group(1), finding.get("detail", ""))
    detail = finding.get("detail", "")
    text = detail if line <= 0 else file_line_text(root, relpath, line)
    return ("line", relpath, rule, line, text)


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
    if file_path in new_files:
        return True
    line = int(finding.get("line") or 0)
    line_changed = line in paths.get(file_path, set())
    if _FUNC_NAME.match(finding.get("detail", "")) and line > 0:
        return is_new and (finding.get("changeContext") == "changed-line" or line_changed)
    return is_new or line_changed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aislop_changed_gate.py <merge-base>")
    base = sys.argv[1]

    files = changed_files(base)
    paths, new_files = added_lines(base)
    config_files = [".aislop/config.yml", ".aislop/rules.yml"]

    config_path = _ROOT / ".aislop/config.yml"
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
    if score_worsened_since_base(base_report, head, config_path):
        blocking.append(
            {
                "rule": "ci.failBelow",
                "severity": "error",
                "file": "<repo>",
                "line": None,
                "detail": f"score={head.get('score')}",
                "message": "aislop score is below ci.failBelow",
            }
        )

    base_threshold_broken = score_blocking(base_report, config_path)
    head_threshold_broken = score_blocking(head, config_path)

    threshold = aislop_fail_below(config_path)
    summary = {
        "base": base,
        "changed_files": len(files),
        "head_diagnostics": len(head_diags),
        "base_diagnostics": len(base_diags),
        "new_diagnostics": len(new),
        "head_score": head.get("score"),
        "base_score": base_report.get("score"),
        "head_score_below_threshold": head_threshold_broken,
        "base_score_below_threshold": base_threshold_broken,
        "head_fail_below": threshold,
        "blocking_diagnostics": len(blocking),
        "blocking": [
            {
                "file": d.get("filePath") or d.get("file"),
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
