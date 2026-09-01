"""The corpus / training seam, enforced.

README states the invariant: question content is rebuildable from raw sources
or from the JSONL archive alone, while attempt history exists only in
data/satprep.db. Before this split nothing in the code said so - corpus and
training modules imported each other freely, and 32 function-local imports
existed to dodge the resulting cycle.

These tests are the acceptance criteria for that split. They read the import
graph rather than the runtime, so they fail on the offending line rather than
on some later symptom.
"""

import ast
import pathlib

import pytest

SATPREP = pathlib.Path(__file__).resolve().parent.parent / "satprep"

#: Imports that may stay inside a function body. Everything else at function
#: scope is a cycle that was dodged rather than broken.
ALLOWED_DEFERRED = {
    "uvicorn",  # optional heavyweight dependency, only `satprep serve` needs it
}


def _modules(package: str | None = None):
    base = SATPREP / package if package else SATPREP
    for path in sorted(base.glob("*.py")):
        if path.name == "__init__.py":
            continue
        yield path, ast.parse(path.read_text())


def _imported_names(node: ast.AST):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Import):
            for alias in sub.names:
                yield alias.name
        elif isinstance(sub, ast.ImportFrom):
            yield ("." * sub.level) + (sub.module or "")


def test_corpus_never_imports_training():
    """The dependency runs one way. A drill is made of questions; a question
    knows nothing about drills."""
    offenders = []
    for path, tree in _modules("corpus"):
        for name in _imported_names(tree):
            if "training" in name:
                offenders.append(f"{path.name}: {name}")
    assert offenders == [], "satprep.corpus must not depend on satprep.training: " + "; ".join(
        offenders
    )


def test_corpus_and_training_do_not_import_entry_points():
    """cli, server and analytics consume both packages; neither package may
    reach back up into them."""
    offenders = []
    for package in ("corpus", "training"):
        for path, tree in _modules(package):
            for name in _imported_names(tree):
                if any(entry in name for entry in ("cli", "server", "analytics")):
                    offenders.append(f"{package}/{path.name}: {name}")
    assert offenders == []


@pytest.mark.parametrize("package", [None, "corpus", "training"])
def test_no_imports_hidden_inside_functions(package):
    """The real acceptance criterion for the split.

    An import that cannot sit at module scope means two modules still depend
    on each other and the cycle was worked around, not removed. Only a
    genuinely optional dependency earns an exemption.
    """
    offenders = []
    for path, tree in _modules(package):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    names = list(_imported_names(sub))
                    if any(n.split(".")[0] in ALLOWED_DEFERRED for n in names):
                        continue
                    offenders.append(f"{path.name}:{sub.lineno} in {node.name}() -> {names}")
    assert offenders == [], "deferred imports remain: " + "; ".join(offenders)


def test_every_module_imports_cleanly_on_its_own():
    """A cycle can hide behind import order: the package works when imported
    one way and fails the other. Import each module first, in isolation."""
    import subprocess
    import sys

    modules = []
    for package in (None, "corpus", "training"):
        prefix = f"satprep.{package}." if package else "satprep."
        for path, _ in _modules(package):
            modules.append(prefix + path.stem)

    failures = []
    for module in modules:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            cwd=SATPREP.parent,
        )
        if result.returncode != 0:
            # a non-zero exit with no stderr would otherwise IndexError here,
            # hiding the import failure behind an unrelated one
            lines = result.stderr.strip().splitlines()
            failures.append(f"{module}: {lines[-1] if lines else '(no stderr)'}")
    assert failures == []


def test_effective_tags_view_matches_the_origin_vocabulary():
    """The view's DDL lives in db.SCHEMA (it is storage) while the meaning of
    `origin` lives in corpus.tags. This pins the one literal they share."""
    from satprep import db
    from satprep.corpus import tags

    assert tags.EFFECTIVE_TAGS == db.EFFECTIVE_TAGS
    assert f"origin != '{tags.ORIGIN_SUPPRESSED}'" in db.SCHEMA


def test_session_id_gives_up_rather_than_spinning():
    """A predicate that always reports a collision means the caller is
    broken; looping forever would hide that behind a hang."""
    from satprep.ids import MAX_ID_ATTEMPTS, session_id

    calls = []

    with pytest.raises(RuntimeError, match="collision check"):
        session_id("hard_mixed", "seed", exists=lambda c: calls.append(c) or True)

    assert len(calls) == MAX_ID_ATTEMPTS
    assert len(set(calls)) == MAX_ID_ATTEMPTS  # each attempt is freshly salted


def test_session_id_is_stable_and_collision_free():
    from satprep.ids import session_id

    assert session_id("hard_mixed", "s") == session_id("hard_mixed", "s")
    assert session_id("hard_mixed", "s") != session_id("error_clinic", "s")

    taken = {session_id("hard_mixed", "s")}
    assert session_id("hard_mixed", "s", exists=taken.__contains__) not in taken
