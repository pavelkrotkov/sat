import copy
from pathlib import Path
from typing import cast

import pytest

from scripts import aislop_changed_gate as gate
from scripts import aislop_policy as policy
from scripts import ruff_changed_gate as ruff_gate


def test_default_config_fallback_is_detected_in_command_output():
    output = "Using default configuration, ignoring custom rules."
    assert "using default configuration" in output.lower()


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _fake_diff_output(output: str):
    def _fake_run(cmd, check, capture_output, text, **kwargs):
        return _Completed(output)

    return _fake_run


def test_added_lines_tracks_only_added_hunk_lines(monkeypatch):
    diff = """diff --git a/demo.py b/demo.py
index 111111..222222 100644
--- a/demo.py
+++ b/demo.py
@@ -184,1 +184,1 @@
+added line
"""

    monkeypatch.setattr(gate.subprocess, "run", _fake_diff_output(diff))
    ranges, new_files = gate.added_lines("base")

    assert new_files == set()
    assert ranges["demo.py"] == {184}


def test_finding_signature_uses_detail_for_metric_lines():
    base = {
        "filePath": "satprep/corpus/audit.py",
        "line": 0,
        "rule": "complexity/file-too-large",
        "detail": "satprep/corpus/audit.py · 858 lines",
    }
    head = {
        **base,
        "detail": "satprep/corpus/audit.py · 859 lines",
    }

    assert gate.finding_signature(base, ".") != gate.finding_signature(head, ".")
    assert gate.is_changed_finding(
        head,
        {},
        set(),
        is_new=gate.finding_signature(head, ".") != gate.finding_signature(base, "."),
    )


def test_score_worsened_since_base_only_blocks_regressions(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text("\n".join(["ci:", "  failBelow: 85"]), encoding="utf-8")

    assert gate.score_worsened_since_base({"score": 90}, {"score": 84}, config)
    assert not gate.score_worsened_since_base({"score": 90}, {"score": 86}, config)
    assert not gate.score_worsened_since_base({"score": 63}, {"score": 64}, config)
    assert not gate.score_worsened_since_base({"score": 63}, {"score": 63}, config)


def test_score_worsened_missing_base_score_fails_closed(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text("\n".join(["ci:", "  failBelow: 85"]), encoding="utf-8")

    with pytest.raises(SystemExit):
        gate.score_worsened_since_base({}, {"score": 84}, config)


def test_score_blocking_reads_ci_failbelow(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text(
        "\n".join(
            [
                "ci:",
                "  failBelow: 85",
            ]
        ),
        encoding="utf-8",
    )

    assert gate.score_blocking({"score": 84}, config)
    assert not gate.score_blocking({"score": 85}, config)
    assert not gate.score_blocking({"score": 86}, config)
    finding = {
        "filePath": "satprep/corpus/audit.py",
        "line": 47,
        "changeContext": "changed-line",
        "detail": "audit_bluebook · 147 lines",
    }

    assert gate.is_changed_finding(finding, {}, set(), is_new=True)


def test_unchanged_legacy_function_finding_does_not_block():
    finding = {
        "filePath": "satprep/corpus/audit.py",
        "line": 47,
        "changeContext": "changed-line",
        "detail": "audit_bluebook · 147 lines",
    }

    assert not gate.is_changed_finding(finding, {}, set(), is_new=False)


def test_worsened_function_threshold_blocks_with_unchanged_anchor():
    base = {
        "filePath": "satprep/corpus/audit.py",
        "rule": "complexity/function-too-long",
        "line": 47,
        "detail": "audit_bluebook · 146 lines",
    }
    head = {
        **base,
        "changeContext": "changed-line",
        "detail": "audit_bluebook · 147 lines",
    }

    is_new = gate.finding_signature(head, ".") != gate.finding_signature(base, ".")

    assert gate.is_changed_finding(head, {}, set(), is_new=is_new)
    assert gate.is_blocking(head)


def test_deep_nesting_threshold_is_blocking():
    finding = {
        "rule": "complexity/deep-nesting",
        "severity": "warning",
    }

    assert gate.is_blocking(finding)


def test_policy_rejects_schema_invalid_config(tmp_path: Path):
    config = tmp_path / "config.yml"
    config.write_text("version: 1\nengines:\n  unknown: true\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="unknown keys"):
        policy.validate_config(config)


def test_policy_rejects_weaker_enforcement_fields():
    base = cast(
        dict,
        {
            "config": copy.deepcopy(policy.DEFAULT_POLICY),
            "rules": [],
            "suppressions": {"ignore": None, "inline": ()},
        },
    )
    base_config = cast(dict, base["config"])
    cast(dict, base_config["lint"])["typecheck"] = True
    cases = [
        (("lint", "typecheck"), False),
        (("security", "audit"), False),
        (("scoring", "weights", "security"), 0),
    ]

    for path, value in cases:
        head = cast(dict, copy.deepcopy(base))
        target = cast(dict, head["config"])
        for key in path[:-1]:
            target = cast(dict, target[key])
        target[path[-1]] = value
        with pytest.raises(SystemExit):
            policy.ensure_policy_not_weakened(base, head)


def test_policy_rejects_new_disabled_rule_override():
    base = cast(
        dict,
        {
            "config": copy.deepcopy(policy.DEFAULT_POLICY),
            "rules": [],
            "suppressions": {"ignore": None, "inline": ()},
        },
    )
    head = cast(dict, copy.deepcopy(base))
    cast(dict, head["config"])["rules"] = {"ai-slop/new-rule": "off"}

    with pytest.raises(SystemExit, match="disabled rule override"):
        policy.ensure_policy_not_weakened(base, head)


def _policy_directory(root: Path, *, ignore: str | None = None, inline: bool = False) -> None:
    (root / ".aislop").mkdir(parents=True)
    (root / ".aislop" / "config.yml").write_text("{}\n", encoding="utf-8")
    if ignore is not None:
        (root / ".aislopignore").write_text(ignore, encoding="utf-8")
    if inline:
        marker = "aislop-" + "ignore"
        (root / "module.py").write_text(f"value = 1  # {marker}\n", encoding="utf-8")


def test_policy_rejects_new_ignore_file(tmp_path: Path):
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    _policy_directory(base_dir)
    _policy_directory(head_dir, ignore="satprep/**\n")

    with pytest.raises(SystemExit, match=r"\.aislopignore"):
        policy.ensure_policy_not_weakened(
            policy.load_policy(str(base_dir)), policy.load_policy(str(head_dir))
        )


def test_policy_rejects_new_inline_suppression(tmp_path: Path):
    base_dir = tmp_path / "base"
    head_dir = tmp_path / "head"
    _policy_directory(base_dir)
    _policy_directory(head_dir, inline=True)

    with pytest.raises(SystemExit, match="inline aislop suppressions"):
        policy.ensure_policy_not_weakened(
            policy.load_policy(str(base_dir)), policy.load_policy(str(head_dir))
        )


def test_changed_c901_matches_body_edits(monkeypatch, tmp_path: Path):
    source = tmp_path / "demo.py"
    source.write_text("def legacy():\n    return 1\n", encoding="utf-8")
    diagnostic = {
        "filename": str(source),
        "location": {"row": 1},
        "message": "`legacy` is too complex (11 > 10)",
    }
    monkeypatch.setattr(ruff_gate, "_ruff_c901", lambda directory: [diagnostic])

    assert ruff_gate.changed_c901(str(tmp_path), {"demo.py": {2}}, set()) == [diagnostic]
    assert ruff_gate.changed_c901(str(tmp_path), {"demo.py": {3}}, set()) == []
