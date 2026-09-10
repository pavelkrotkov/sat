from pathlib import Path

import pytest

from scripts import aislop_changed_gate as gate


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
