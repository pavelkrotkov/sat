from __future__ import annotations

import copy
import json
from typing import cast

from scripts.aislop_policy import (
    DEFAULT_ENGINES,
    DEFAULT_EXCLUDE,
    DEFAULT_POLICY,
    REQUIRED_QUALITY,
    REQUIRED_RULES,
    _fail,
)

BUILTIN_ERROR_RULES = {
    "ai-slop/hallucinated-import",
    "security/hardcoded-secret",
    "security/vulnerable-dependency",
    "security/eval",
    "security/innerhtml",
    "security/dangerously-set-innerhtml",
    "security/sql-injection",
    "security/shell-injection",
    "security/unsafe-deserialization",
    "security/unsafe-c-call",
}


def _merge_defaults(defaults: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _effective(policy: dict) -> dict:
    config = policy["config"]
    return {
        "engines": _merge_defaults(DEFAULT_ENGINES, config.get("engines") or {}),
        "quality": _merge_defaults(REQUIRED_QUALITY, config.get("quality") or {}),
        "lint": _merge_defaults(cast(dict, DEFAULT_POLICY["lint"]), config.get("lint") or {}),
        "security": _merge_defaults(
            cast(dict, DEFAULT_POLICY["security"]), config.get("security") or {}
        ),
        "scoring": _merge_defaults(
            cast(dict, DEFAULT_POLICY["scoring"]), config.get("scoring") or {}
        ),
        "ci": _merge_defaults(cast(dict, DEFAULT_POLICY["ci"]), config.get("ci") or {}),
        "telemetry": _merge_defaults(
            cast(dict, DEFAULT_POLICY["telemetry"]), config.get("telemetry") or {}
        ),
        "rules": config.get("rules", {}),
        "exclude": set(config.get("exclude", DEFAULT_EXCLUDE)),
        "architecture_rules": policy["rules"],
    }


def _strength(value: object) -> int:
    return {"off": 0, "warning": 1, "error": 2}.get(value if isinstance(value, str) else "", 1)


def _flatten_enforcement(value: object, path: tuple[str, ...]) -> dict[tuple[str, ...], object]:
    if not isinstance(value, dict):
        return {path: value}
    flattened: dict[tuple[str, ...], object] = {}
    for key, item in value.items():
        flattened.update(_flatten_enforcement(item, (*path, str(key))))
    return flattened


def _ensure_numeric_not_weaker(old: float, new: float, field: tuple[str, ...]) -> None:
    if field[0] == "quality":
        if new > old:
            _fail(f"head raises the base quality.{field[1]} limit")
        return
    if field == ("ci", "failBelow"):
        if new < old:
            _fail("head lowers the base ci.failBelow threshold")
        return
    if field[:2] in (("scoring", "weights"), ("scoring", "thresholds")):
        if new < old:
            _fail(f"head lowers the base scoring.{field[1]}.{field[2]} value")
        return
    if field == ("scoring", "maxPerRule"):
        if new > old:
            _fail("head raises the base scoring.maxPerRule limit")
        return
    if new != old:
        _fail(f"head changes enforcement field {'.'.join(field)}")


def _ensure_enforcement_not_weakened(old: object, new: object, path: tuple[str, ...]) -> None:
    old_fields = _flatten_enforcement(old, path)
    new_fields = _flatten_enforcement(new, path)
    if set(old_fields) != set(new_fields):
        removed = set(old_fields) - set(new_fields)
        field = next(iter(removed or (set(new_fields) - set(old_fields))))
        action = "removes" if removed else "adds"
        _fail(f"head {action} enforcement field {'.'.join(field)}")
    for field, old_value in old_fields.items():
        new_value = new_fields[field]
        if isinstance(old_value, bool):
            if old_value and new_value is not True:
                _fail(f"head disables enforcement field {'.'.join(field)}")
        elif isinstance(old_value, (int, float)) and isinstance(new_value, (int, float)):
            _ensure_numeric_not_weaker(old_value, new_value, field)
        elif old_value != new_value:
            _fail(f"head changes enforcement field {'.'.join(field)}")


def _ensure_policy_fields(old: dict, new: dict) -> None:
    for key in ("engines", "quality", "lint", "security", "scoring", "ci"):
        _ensure_enforcement_not_weakened(old[key], new[key], (key,))
    if new["telemetry"]["enabled"] is not False:
        _fail("head enables aislop telemetry")


def _ensure_rule_changes(old: dict, new: dict) -> None:
    for rule, severity in old["rules"].items():
        if _strength(new["rules"].get(rule, "warning")) < _strength(severity):
            _fail(f"head weakens the base rule {rule}")
    for rule, severity in new["rules"].items():
        if rule in BUILTIN_ERROR_RULES and _strength(severity) < _strength("error"):
            _fail(f"head weakens aislop built-in error rule: {rule}")
        if severity == "off" and old["rules"].get(rule, "warning") != "off":
            _fail(f"head adds a disabled rule override: {rule}")


def _ensure_architecture_and_excludes(old: dict, new: dict) -> None:
    old_rules = {json.dumps(rule, sort_keys=True) for rule in old["architecture_rules"]}
    new_rules = {json.dumps(rule, sort_keys=True) for rule in new["architecture_rules"]}
    if not old_rules <= new_rules:
        _fail("head removes an architecture rule from the base")
    if not new["exclude"] <= old["exclude"]:
        _fail("head adds an excluded path")


def _ensure_suppressions_unchanged(base: dict, head: dict) -> None:
    old_suppressions = base.get("suppressions", {})
    new_suppressions = head.get("suppressions", {})
    if new_suppressions.get("ignore") not in (None, old_suppressions.get("ignore")):
        _fail("head changes .aislopignore; ignore-file changes are not allowed")
    old_inline = set(old_suppressions.get("inline", ()))
    new_inline = set(new_suppressions.get("inline", ()))
    if not new_inline <= old_inline:
        _fail("head adds inline aislop suppressions")


def ensure_policy_not_weakened(base: dict, head: dict) -> None:
    old = _effective(base)
    new = _effective(head)
    _ensure_policy_fields(old, new)
    _ensure_rule_changes(old, new)
    _ensure_architecture_and_excludes(old, new)
    _ensure_suppressions_unchanged(base, head)


def _ensure_required_engines(effective: dict) -> None:
    for engine in ("format", "lint", "code-quality", "ai-slop", "architecture", "security"):
        if not effective["engines"].get(engine, False):
            _fail(f"required engine is disabled: {engine}")


def _ensure_required_quality(effective: dict) -> None:
    for key, limit in REQUIRED_QUALITY.items():
        if effective["quality"].get(key, limit) > limit:
            _fail(f"quality.{key} exceeds the required limit")
    if effective["ci"]["failBelow"] < 85:
        _fail("ci.failBelow must be at least 85")


def _ensure_required_rules(effective: dict) -> None:
    for rule in REQUIRED_RULES:
        if effective["rules"].get(rule) != "error":
            _fail(f"required rule is not blocking: {rule}")
    for rule in BUILTIN_ERROR_RULES:
        if _strength(effective["rules"].get(rule, "error")) < _strength("error"):
            _fail(f"built-in error rule is not blocking: {rule}")


def ensure_required_policy(policy: dict) -> None:
    effective = _effective(policy)
    _ensure_required_engines(effective)
    _ensure_required_quality(effective)
    _ensure_required_rules(effective)
    if effective["telemetry"]["enabled"] is not False:
        _fail("aislop telemetry must be disabled")
