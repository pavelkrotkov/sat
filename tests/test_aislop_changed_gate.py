from scripts import aislop_changed_gate as gate


def test_function_finding_uses_aislop_changed_span():
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
