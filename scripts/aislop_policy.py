from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NoReturn, cast

import yaml

DEFAULT_EXCLUDE = {"node_modules", ".git", "dist", "build", "coverage"}
DEFAULT_ENGINES = {
    "format": True,
    "lint": True,
    "code-quality": True,
    "ai-slop": True,
    "architecture": False,
    "security": True,
}
REQUIRED_QUALITY = {
    "maxFunctionLoc": 80,
    "maxFileLoc": 400,
    "maxNesting": 5,
    "maxParams": 6,
}
REQUIRED_RULES = {
    "ai-slop/narrative-comment": "error",
    "ai-slop/trivial-comment": "error",
    "ai-slop/meta-comment": "error",
    "ai-slop/todo-stub": "error",
    "ai-slop/swallowed-exception": "error",
    "ai-slop/silent-recovery": "error",
    "ai-slop/hidden-fallback": "error",
}
DEFAULT_POLICY = {
    "version": 1,
    "engines": DEFAULT_ENGINES,
    "quality": REQUIRED_QUALITY,
    "lint": {
        "typecheck": False,
        "expoDoctor": False,
    },
    "security": {
        "audit": True,
        "auditTimeout": 25000,
    },
    "scoring": {
        "weights": {
            "format": 0.3,
            "lint": 0.6,
            "code-quality": 0.8,
            "ai-slop": 1,
            "architecture": 1,
            "security": 1.5,
        },
        "thresholds": {"good": 75, "ok": 50},
        "smoothing": 5,
        "maxPerRule": 40,
    },
    "ci": {"failBelow": 70, "format": "json"},
    "telemetry": {"enabled": True},
    "rules": {},
}

_TOP_LEVEL = {
    "version",
    "engines",
    "quality",
    "lint",
    "security",
    "scoring",
    "ci",
    "telemetry",
    "rules",
    "exclude",
    "include",
}
_SEVERITIES = {"error", "warning", "off"}
_JB_SEVERITIES = {"ERROR", "WARNING", "SUGGESTION", "HINT"}


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"invalid aislop policy: {message}")


def _mapping(value: object, label: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        _fail(f"{label} must be a mapping")
    mapping = cast(dict, value)
    if allowed is not None:
        unknown = set(mapping) - allowed
        if unknown:
            _fail(f"{label} has unknown keys: {sorted(unknown)}")
    return mapping


def _number(
    value: object,
    label: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        suffix = "positive finite number" if positive else "non-negative finite number"
        _fail(f"{label} must be a {suffix}")


def _boolean(value: object, label: str) -> None:
    if not isinstance(value, bool):
        _fail(f"{label} must be boolean")


def _strings(value: object, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{label} must be a list of strings")


def _validate_lint(value: object) -> None:
    lint = _mapping(
        value,
        "lint",
        {"typecheck", "expoDoctor", "csharp", "cpp"},
    )
    for key in ("typecheck", "expoDoctor"):
        if key in lint:
            _boolean(lint[key], f"lint.{key}")
    for name, allowed in {
        "csharp": {
            "projectEvaluation",
            "jb",
            "roslynator",
            "jbSeverityFloor",
            "jbExcludeTypes",
            "jbProjects",
        },
        "cpp": {
            "cppcheck",
            "clangTidy",
            "cppcheckEnable",
            "jb",
            "jbProjects",
            "jbSeverityFloor",
            "jbExcludeTypes",
        },
    }.items():
        if name not in lint:
            continue
        section = _mapping(lint[name], f"lint.{name}", allowed)
        for key in {
            "projectEvaluation",
            "jb",
            "roslynator",
            "cppcheck",
            "clangTidy",
        } & set(section):
            _boolean(section[key], f"lint.{name}.{key}")
        if "jbSeverityFloor" in section and section["jbSeverityFloor"] not in _JB_SEVERITIES:
            _fail(f"lint.{name}.jbSeverityFloor has an invalid value")
        if "jbExcludeTypes" in section:
            _strings(section["jbExcludeTypes"], f"lint.{name}.jbExcludeTypes")
        for key in ("jbProjects", "cppcheckEnable"):
            if key in section and not isinstance(section[key], str):
                _fail(f"lint.{name}.{key} must be a string")


def _validate_engines(value: object) -> None:
    engines = _mapping(value, "engines", set(DEFAULT_ENGINES))
    for key, item in engines.items():
        _boolean(item, f"engines.{key}")


def _validate_quality(value: object) -> None:
    quality = _mapping(value, "quality", set(REQUIRED_QUALITY))
    for key, item in quality.items():
        _number(item, f"quality.{key}", positive=True)


def _validate_security(value: object) -> None:
    security = _mapping(value, "security", {"audit", "auditTimeout"})
    if "audit" in security:
        _boolean(security["audit"], "security.audit")
    if "auditTimeout" in security:
        _number(security["auditTimeout"], "security.auditTimeout", positive=True)


def _validate_scoring(value: object) -> None:
    scoring = _mapping(
        value,
        "scoring",
        {"weights", "thresholds", "smoothing", "maxPerRule"},
    )
    if "weights" in scoring:
        weights = _mapping(scoring["weights"], "scoring.weights")
        for key, item in weights.items():
            if not isinstance(key, str):
                _fail("scoring.weights keys must be strings")
            _number(item, f"scoring.weights.{key}")
    thresholds = _mapping(scoring.get("thresholds", {}), "scoring.thresholds", {"good", "ok"})
    for key, item in thresholds.items():
        _number(item, f"scoring.thresholds.{key}")
    if "smoothing" in scoring:
        _number(scoring["smoothing"], "scoring.smoothing", nonnegative=True)
    if "maxPerRule" in scoring:
        _number(scoring["maxPerRule"], "scoring.maxPerRule", positive=True)


def _validate_ci(value: object) -> None:
    ci = _mapping(value, "ci", {"failBelow", "format"})
    if "failBelow" in ci:
        _number(ci["failBelow"], "ci.failBelow")
    if "format" in ci and ci["format"] != "json":
        _fail("ci.format must be json")


def _validate_overrides(value: object) -> None:
    rules = _mapping(value, "rules")
    for key, item in rules.items():
        if not isinstance(key, str) or item not in _SEVERITIES:
            _fail("rules must map string rule ids to error, warning, or off")


def _validate_paths(config: dict) -> None:
    for key in ("exclude", "include"):
        if key in config:
            _strings(config[key], key)
    if config.get("include", []):
        _fail("include is not supported by the changed-code gate")
    if set(config.get("exclude", DEFAULT_EXCLUDE)) != DEFAULT_EXCLUDE:
        _fail("exclude must use aislop's default paths")


def _validate_config(value: object) -> dict:
    config = _mapping(value, "configuration", _TOP_LEVEL)
    if "version" in config:
        _number(config["version"], "version", positive=True)
        if config["version"] != 1:
            _fail("version must be 1")
    _validate_engines(config.get("engines", {}))
    _validate_quality(config.get("quality", {}))
    _validate_lint(config.get("lint", {}))
    _validate_security(config.get("security", {}))
    _validate_scoring(config.get("scoring", {}))
    _validate_ci(config.get("ci", {}))
    telemetry = _mapping(config.get("telemetry", {}), "telemetry", {"enabled"})
    if "enabled" in telemetry:
        _boolean(telemetry["enabled"], "telemetry.enabled")
    _validate_overrides(config.get("rules", {}))
    _validate_paths(config)
    return config


def validate_config(config_path: Path) -> dict:
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"unable to read {config_path}: {exc}")
    except yaml.YAMLError as exc:
        _fail(f"invalid YAML in {config_path}: {exc}")
    if raw is None:
        _fail("configuration is empty")
    return _validate_config(raw)


def validate_rules(rules_path: Path) -> list[dict]:
    if not rules_path.exists():
        return []
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"unable to read {rules_path}: {exc}")
    except yaml.YAMLError as exc:
        _fail(f"invalid YAML in {rules_path}: {exc}")
    if raw is None:
        return []
    config = _mapping(raw, "architecture rules", {"rules"})
    rules = config.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, dict) for rule in rules):
        _fail("architecture rules.rules must be a list of mappings")
    return cast(list[dict], rules)


def load_policy(directory: str) -> dict:
    root = Path(directory)
    config_path = root / ".aislop" / "config.yml"
    if not config_path.is_file():
        _fail(f"missing {config_path}")
    return {
        "config": validate_config(config_path),
        "rules": validate_rules(root / ".aislop" / "rules.yml"),
    }


def validate_report(report: object, policy: dict) -> dict:
    if not isinstance(report, dict):
        raise SystemExit("aislop returned a non-object report")
    if (
        report.get("schemaVersion") != "1"
        or report.get("cliVersion") != "0.16.0"
        or report.get("version") != "0.16.0"
    ):
        raise SystemExit("aislop returned an unexpected report schema or CLI version")
    score = report.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise SystemExit("aislop report has an invalid score")
    diagnostics = report.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(isinstance(item, dict) for item in diagnostics):
        raise SystemExit("aislop report is missing diagnostics")
    engines = report.get("engines")
    if not isinstance(engines, dict):
        raise SystemExit("aislop report is missing engine results")
    for engine, active in policy["config"].get("engines", {}).items():
        if active and engine not in engines:
            raise SystemExit(f"aislop report is missing enabled engine: {engine}")
    if not isinstance(report.get("summary"), dict) or report.get("scoreable") is not True:
        raise SystemExit("aislop report is not scoreable")
    return report


def write_default_policy(directory: str) -> None:
    path = Path(directory) / ".aislop" / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(DEFAULT_POLICY, sort_keys=False), encoding="utf-8")


def _effective(policy: dict) -> dict:
    config = policy["config"]
    engines = config.get("engines") or {}
    quality = config.get("quality") or {}
    ci = config.get("ci") or {}
    return {
        "engines": {key: engines.get(key, default) for key, default in DEFAULT_ENGINES.items()},
        "quality": {key: quality.get(key, default) for key, default in REQUIRED_QUALITY.items()},
        "failBelow": ci.get("failBelow", 70),
        "rules": config.get("rules", {}),
        "exclude": set(config.get("exclude", DEFAULT_EXCLUDE)),
        "architecture_rules": policy["rules"],
    }


def _strength(value: object) -> int:
    return {"off": 0, "warning": 1, "error": 2}.get(value if isinstance(value, str) else "", 1)


def ensure_policy_not_weakened(base: dict, head: dict) -> None:
    old = _effective(base)
    new = _effective(head)
    for engine, enabled in old["engines"].items():
        if enabled and not new["engines"].get(engine, False):
            _fail(f"head disables the base {engine} engine")
    for key, limit in old["quality"].items():
        if new["quality"].get(key, limit) > limit:
            _fail(f"head raises the base quality.{key} limit")
    if new["failBelow"] < old["failBelow"]:
        _fail("head lowers the base ci.failBelow threshold")
    for rule, severity in old["rules"].items():
        if _strength(new["rules"].get(rule, "warning")) < _strength(severity):
            _fail(f"head weakens the base rule {rule}")
    old_rules = {json.dumps(rule, sort_keys=True) for rule in old["architecture_rules"]}
    new_rules = {json.dumps(rule, sort_keys=True) for rule in new["architecture_rules"]}
    if not old_rules <= new_rules:
        _fail("head removes an architecture rule from the base")
    if not new["exclude"] <= old["exclude"]:
        _fail("head adds an excluded path")


def ensure_required_policy(policy: dict) -> None:
    effective = _effective(policy)
    for engine in ("format", "lint", "code-quality", "ai-slop", "architecture", "security"):
        if not effective["engines"].get(engine, False):
            _fail(f"required engine is disabled: {engine}")
    for key, limit in REQUIRED_QUALITY.items():
        if effective["quality"].get(key, limit) > limit:
            _fail(f"quality.{key} exceeds the required limit")
    if effective["failBelow"] < 85:
        _fail("ci.failBelow must be at least 85")
    for rule in REQUIRED_RULES:
        if effective["rules"].get(rule) != "error":
            _fail(f"required rule is not blocking: {rule}")
