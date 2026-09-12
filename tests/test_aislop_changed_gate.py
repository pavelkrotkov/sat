import copy
import json
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


def test_changed_file_paths_preserves_rename_pairs(monkeypatch):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        _fake_diff_output("R100\tsatprep/config.py\tsatprep/renamed.py\n"),
    )

    files, renames = gate.changed_file_paths("base")

    assert files == ["satprep/renamed.py"]
    assert renames == {"satprep/config.py": "satprep/renamed.py"}


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


def test_improved_metric_does_not_block():
    base = {
        "filePath": "satprep/config.py",
        "rule": "complexity/file-too-large",
        "detail": "satprep/config.py · 858 lines",
    }
    head = {**base, "detail": "satprep/config.py · 857 lines"}

    assert gate._metric_improved(head, [base], {})


def test_renamed_finding_uses_canonical_path_for_baseline_signature():
    base = {
        "filePath": "satprep/config.py",
        "line": 0,
        "rule": "complexity/file-too-large",
        "detail": "satprep/config.py · 858 lines",
    }
    head = {
        **base,
        "filePath": "satprep/renamed.py",
        "detail": "satprep/renamed.py · 858 lines",
    }

    assert gate.finding_signature(
        base,
        ".",
        canonical_path=head["filePath"],
    ) == gate.finding_signature(head, ".")


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


def test_body_edit_marks_unchanged_function_finding_as_changed(tmp_path: Path):
    source = tmp_path / "demo.py"
    source.write_text(
        "def legacy():\n    value = 1\n    return value\n",
        encoding="utf-8",
    )
    finding = {
        "filePath": "demo.py",
        "line": 1,
        "detail": "legacy · 81 lines",
    }

    assert gate.is_changed_finding(
        finding,
        {"demo.py": {2}},
        set(),
        is_new=False,
        root=str(tmp_path),
    )


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


def test_legacy_file_size_is_advisory_but_new_file_size_blocks():
    finding = {
        "filePath": "satprep/legacy.py",
        "rule": "complexity/file-too-large",
        "severity": "warning",
    }

    assert not gate.is_blocking(finding)
    assert gate.is_blocking(finding, {"satprep/legacy.py"})


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


def test_policy_rejects_lower_max_per_rule_cap():
    base = cast(
        dict,
        {
            "config": copy.deepcopy(policy.DEFAULT_POLICY),
            "rules": [],
            "suppressions": {"ignore": None, "inline": ()},
        },
    )
    head = cast(dict, copy.deepcopy(base))
    cast(dict, head["config"])["scoring"]["maxPerRule"] = 1

    with pytest.raises(SystemExit, match="maxPerRule"):
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


def test_policy_rejects_builtin_error_downgrade():
    base = {
        "config": copy.deepcopy(policy.DEFAULT_POLICY),
        "rules": [],
        "suppressions": {"ignore": None, "inline": ()},
    }
    head = copy.deepcopy(base)
    cast(dict, head["config"])["rules"] = {"security/eval": "warning"}

    with pytest.raises(SystemExit, match="built-in error rule"):
        policy.ensure_policy_not_weakened(base, head)


def test_default_policy_disables_telemetry(tmp_path: Path):
    gate.write_default_policy(str(tmp_path))

    assert "enabled: false" in (tmp_path / ".aislop" / "config.yml").read_text()


def test_policy_allows_disabling_base_telemetry():
    base = {
        "config": copy.deepcopy(policy.DEFAULT_POLICY),
        "rules": [],
        "suppressions": {"ignore": None, "inline": ()},
    }
    cast(dict, base["config"])["telemetry"]["enabled"] = True
    head = copy.deepcopy(base)
    cast(dict, head["config"])["telemetry"]["enabled"] = False

    policy.ensure_policy_not_weakened(base, head)


def test_run_aislop_disables_telemetry(monkeypatch, tmp_path: Path):
    _policy_directory(tmp_path)
    report = {"schemaVersion": "1", "cliVersion": "0.16.0", "version": "0.16.0"}
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return _Completed(json.dumps(report))

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    monkeypatch.setattr(gate, "validate_report", lambda value, _policy: value)

    assert gate.run_aislop(str(tmp_path)) == report
    assert captured["env"]["AISLOP_NO_TELEMETRY"] == "1"


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


def test_report_rejects_skipped_enabled_engine():
    config = copy.deepcopy(policy.DEFAULT_POLICY)
    engines_config = cast(dict[str, bool], config["engines"])
    engines = {name: {} for name, active in engines_config.items() if active}
    engines["ai-slop"] = {"skipped": True}
    report = {
        "schemaVersion": "1",
        "cliVersion": "0.16.0",
        "version": "0.16.0",
        "score": 100,
        "diagnostics": [],
        "engines": engines,
        "summary": {},
        "scoreable": True,
    }

    with pytest.raises(SystemExit, match="skipped enabled engine: ai-slop"):
        policy.validate_report(report, {"config": config, "rules": []})


def test_report_rejects_omitted_required_engine():
    config = copy.deepcopy(policy.DEFAULT_POLICY)
    engines_config = cast(dict[str, bool], config["engines"])
    engines_config.pop("ai-slop")
    engines = {name: {} for name, active in engines_config.items() if active}
    report = {
        "schemaVersion": "1",
        "cliVersion": "0.16.0",
        "version": "0.16.0",
        "score": 100,
        "diagnostics": [],
        "engines": engines,
        "summary": {},
        "scoreable": True,
    }

    with pytest.raises(SystemExit, match="missing enabled engine: ai-slop"):
        policy.validate_report(report, {"config": config, "rules": []})


def test_report_allows_skipped_architecture_without_rules():
    config = copy.deepcopy(policy.DEFAULT_POLICY)
    engines_config = cast(dict[str, bool], config["engines"])
    engines_config["architecture"] = True
    engines = {name: {} for name, active in engines_config.items() if active}
    engines["architecture"] = {"skipped": True}
    report = {
        "schemaVersion": "1",
        "cliVersion": "0.16.0",
        "version": "0.16.0",
        "score": 100,
        "diagnostics": [],
        "engines": engines,
        "summary": {},
        "scoreable": True,
    }

    assert policy.validate_report(report, {"config": config, "rules": []}) == report


def test_ruff_c901_uses_isolated_trusted_limit(monkeypatch, tmp_path: Path):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return _Completed("[]")

    monkeypatch.setattr(ruff_gate.subprocess, "run", fake_run)

    assert ruff_gate._ruff_c901(str(tmp_path)) == []
    command = commands[0]
    assert "--isolated" in command
    assert command[command.index("--config") + 1] == "lint.mccabe.max-complexity=10"


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


def test_changed_c901_does_not_allow_new_def_suppression(monkeypatch, tmp_path: Path):
    source = tmp_path / "demo.py"
    source.write_text("def legacy():  # noqa: C901\n    return 1\n", encoding="utf-8")
    diagnostic = {
        "filename": str(source),
        "location": {"row": 1},
        "message": "`legacy` is too complex (11 > 10)",
    }
    monkeypatch.setattr(ruff_gate, "_ruff_c901", lambda directory: [diagnostic])

    assert ruff_gate.changed_c901(str(tmp_path), {"demo.py": {1}}, set()) == [diagnostic]


def test_trusted_gate_manifest_covers_script_modules_and_uses_safe_runner():
    root = Path(__file__).parents[1]
    manifest = root / ".github" / "aislop-gate.sha256"
    listed = {
        line.split(maxsplit=1)[1]
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    script_modules = {path.relative_to(root).as_posix() for path in (root / "scripts").glob("*.py")}
    assert listed == script_modules

    workflow = (root / ".github" / "workflows" / "aislop.yml").read_text(encoding="utf-8")
    assert "cp .github/aislop-gate.sha256" not in workflow
    assert "trusted_revision=HEAD" not in workflow
    assert "refusing PR-controlled bootstrap" in workflow
    assert 'git archive "$trusted_revision" -- scripts' in workflow
    assert "python -P -m scripts.aislop_changed_gate" in workflow
