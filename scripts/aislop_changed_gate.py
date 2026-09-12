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

import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

from scripts.aislop_policy import (
    ensure_policy_not_weakened,
    ensure_required_policy,
    load_policy,
    validate_report,
    write_default_policy,
)

_ROOT = Path(__file__).resolve().parent.parent
AISLOP = ["npx", "--yes", "aislop@0.16.0", "scan", "--format", "json", "."]
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DEFAULT_CONFIG_WARNING = "using default configuration"
CONFIG_PARSE_WARNING = ("failed to parse", "default configuration")

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
    """Run pinned aislop only after validating its project policy and report."""
    policy = load_policy(directory)
    command = AISLOP
    if base is not None:
        command = [*AISLOP[:-1], "--changes", "--base", base, AISLOP[-1]]
    try:
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    except OSError as exc:
        raise SystemExit(f"aislop command failed: {exc}") from exc

    output = f"{result.stdout}{result.stderr}".lower()
    if DEFAULT_CONFIG_WARNING in output or any(item in output for item in CONFIG_PARSE_WARNING):
        print(result.stderr, file=sys.stderr)
        raise SystemExit("aislop configuration is invalid or fell back to default")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"aislop returned invalid JSON: {exc}") from exc
    return validate_report(report, policy)


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


def materialize_base(base: str, directory: str) -> None:
    """Write the complete base tree without overlaying the PR policy."""
    tree = subprocess.run(
        ["git", "archive", base],
        cwd=_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(tree), mode="r:*") as archive:
        archive.extractall(directory)


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
    use source text when available, which stays stable when code merely shifts lines.
    """
    relpath = finding.get("filePath", "")
    rule = finding.get("rule", "")
    line = int(finding.get("line") or 0)
    func_match = _FUNC_NAME.match(finding.get("detail", ""))
    if func_match and line > 0:
        return ("func", relpath, rule, func_match.group(1), finding.get("detail", ""))
    detail = finding.get("detail", "")
    text = detail if line <= 0 else file_line_text(root, relpath, line)
    return ("line", relpath, rule, text or detail)


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
        return is_new
    return is_new or line_changed


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aislop_changed_gate.py <merge-base>")
    base = sys.argv[1]

    files = changed_files(base)
    paths, new_files = added_lines(base)
    config_path = _ROOT / ".aislop/config.yml"
    head_policy = load_policy(str(_ROOT))
    ensure_required_policy(head_policy)

    with tempfile.TemporaryDirectory(prefix="aislop_base_") as base_dir:
        materialize_base(base, base_dir)
        base_config_path = Path(base_dir) / ".aislop" / "config.yml"
        if not base_config_path.is_file():
            write_default_policy(base_dir)
        base_policy = load_policy(base_dir)
        ensure_policy_not_weakened(base_policy, head_policy)
        head = run_aislop(str(_ROOT))
        base_report = run_aislop(base_dir)
        base_diags = [d for d in base_report.get("diagnostics", []) if d.get("filePath") in files]
        base_sigs = {finding_signature(d, base_dir) for d in base_diags}

    head_diags = [d for d in head.get("diagnostics", []) if d.get("filePath") in files]
    head_sigs = {finding_signature(d, str(_ROOT)) for d in head_diags}
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
    score_comparable = base_policy == head_policy
    head_threshold_broken = score_blocking(head, config_path)
    score_regressed = score_comparable and score_worsened_since_base(base_report, head, config_path)
    if not score_comparable:
        score_regressed = bool(new) and head_threshold_broken
    if score_regressed:
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

    base_threshold_broken = score_blocking(base_report, config_path) if score_comparable else None
    threshold = aislop_fail_below(config_path)
    summary = {
        "base": base,
        "changed_files": len(files),
        "head_diagnostics": len(head_diags),
        "base_diagnostics": len(base_diags),
        "new_diagnostics": len(new),
        "head_score": head.get("score"),
        "base_score": base_report.get("score"),
        "score_comparable": score_comparable,
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
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
