"""The deploy assets, checked without a machine to deploy to.

These files are only ever exercised on the serving box, where a mistake shows
up as a unit that fails to start hours after the change was made. The two
failure modes worth catching here are cheap: a shell script that does not
parse, and a placeholder that install.sh does not know how to substitute.
"""

import pathlib
import re
import subprocess

import pytest

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"

SCRIPTS = sorted(DEPLOY.glob("*.sh"))
UNITS = sorted(list(DEPLOY.glob("*.service")) + list(DEPLOY.glob("*.timer")))

#: What install.sh's render() knows how to replace.
SUBSTITUTED = {"__USER__", "__DIR__", "__UV__", "__HOST__", "__PORT__"}


def test_the_expected_assets_are_present():
    assert {p.name for p in SCRIPTS} == {"backup.sh", "install.sh", "sync-to-hermes.sh"}
    assert {p.name for p in UNITS} == {
        "satprep.service", "satprep-backup.service", "satprep-backup.timer"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_executable_and_fail_loudly(script):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
    # Without -e a failed rsync or sqlite3 call is followed by the next step
    # anyway, which is how a backup script reports success having written
    # nothing.
    assert "set -euo pipefail" in script.read_text()


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_every_unit_placeholder_is_one_install_substitutes(unit):
    """A new placeholder in a unit ships as a literal __NAME__ unless render()
    learns about it, and systemd reports that as a missing executable."""
    found = set(re.findall(r"__[A-Z_]+__", unit.read_text()))
    assert found <= SUBSTITUTED, f"{unit.name} uses {found - SUBSTITUTED}"


def test_install_substitutes_every_placeholder_it_claims_to():
    render = (DEPLOY / "install.sh").read_text()
    for name in SUBSTITUTED:
        assert f"s|{name}|" in render, f"install.sh never substitutes {name}"


def test_install_renders_every_unit_in_the_directory():
    """A unit added to deploy/ but not to install.sh's loop is simply never
    installed, and nothing says so."""
    render = (DEPLOY / "install.sh").read_text()
    for unit in UNITS:
        assert unit.name in render, f"install.sh never renders {unit.name}"


def test_the_service_may_write_the_database_it_owns():
    """ProtectSystem=strict makes the whole filesystem read-only, so the one
    unrebuildable file needs an explicit exception or drills fail to save."""
    unit = (DEPLOY / "satprep.service").read_text()
    assert "ProtectSystem=strict" in unit
    writable = re.search(r"ReadWritePaths=(.+)", unit).group(1)
    assert "__DIR__/data" in writable


def test_backup_refuses_to_rotate_an_empty_database():
    """30 nightly runs against a fresh or half-restored copy would roll every
    real backup off the end of the retention window."""
    body = (DEPLOY / "backup.sh").read_text()
    assert "COUNT(*) FROM attempts" in body
    assert "refusing to rotate backups" in body
    # cp of a live WAL database can capture a torn page.
    assert ".backup" in body and "integrity_check" in body


def _code(path):
    """Executable lines only - the comments explain the rule being checked."""
    return "\n".join(line for line in path.read_text().splitlines()
                     if not line.lstrip().startswith("#"))


def test_sync_never_sends_the_database():
    """hermes owns data/satprep.db. Restore does not carry attempt state, so a
    copy landing on the far side would be an unmergeable second history."""
    body = _code(DEPLOY / "sync-to-hermes.sh")
    assert "satprep.db" not in body
    assert "satprep ingest" in body, "ingest must run on the far side"
