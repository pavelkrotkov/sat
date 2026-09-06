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
    assert {p.name for p in SCRIPTS} == {"backup.sh", "install.sh", "sync-to-server.sh"}
    assert {p.name for p in UNITS} == {
        "satprep.service",
        "satprep-backup.service",
        "satprep-backup.timer",
    }


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_executable_and_fail_loudly(script):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
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
    assert ".backup" in body and "integrity_check" in body


def _code(path):
    """Executable lines only - the comments explain the rule being checked."""
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_sync_never_sends_the_database():
    """The serving box owns data/satprep.db; copying another writable history
    to it would create state that the application cannot merge."""
    body = _code(DEPLOY / "sync-to-server.sh")
    assert "satprep.db" not in body
    assert "satprep ingest" in body, "ingest must run on the far side"


def test_sync_requires_the_actual_server_host():
    body = _code(DEPLOY / "sync-to-server.sh")
    assert "${SATPREP_HOST:?" in body
    assert "satprep.local" not in body


def test_sync_does_not_restart_the_service():
    """server.py takes one connection per request, so a new corpus is visible
    on the next page load without a privileged remote restart."""
    body = _code(DEPLOY / "sync-to-server.sh")
    assert "systemctl restart" not in body


def test_remote_commands_run_through_a_login_shell():
    """uv installs to ~/.local/bin, and a non-interactive ssh command gets the
    system PATH only - Debian's .bashrc returns before the line adding it."""
    body = _code(DEPLOY / "sync-to-server.sh")
    assert "bash -lc" in body


def test_installer_escapes_substitution_values():
    """The values land in a sed replacement, where | is the delimiter and &
    means the whole match. A path holding either would silently corrupt the
    rendered unit instead of failing."""
    body = _code(DEPLOY / "install.sh")
    assert "esc()" in body
    assert body.count('$(esc "') >= len(SUBSTITUTED)


def test_installer_resolves_the_primary_group():
    """A service account's group often differs from its name (nobody/nogroup),
    and `install -g` would read the username as a group."""
    body = _code(DEPLOY / "install.sh")
    assert 'id -gn "$RUN_AS"' in body


def test_service_gives_uv_a_writable_cache():
    """ProtectHome=read-only also covers $HOME/.cache/uv, and --frozen only
    pins the lockfile - uv still updates the project environment. Without both
    halves the unit exits before uvicorn starts."""
    unit = (DEPLOY / "satprep.service").read_text()
    assert "--no-sync" in unit
    assert "CacheDirectory=" in unit and "UV_CACHE_DIR=" in unit
    assert "uv sync --frozen" in _code(DEPLOY / "install.sh")


def test_retention_survives_a_missing_corpus_snapshot():
    """The corpus export is explicitly optional. A glob matching nothing makes
    `ls` exit non-zero, and under pipefail that would fail after a good database
    snapshot had already been written."""
    body = _code(DEPLOY / "backup.sh")
    assert "find " in body
    assert 'ls -1t "$DEST"' not in body


def test_restore_clears_the_wal_sidecars():
    """A -wal newer than the snapshot replays onto it, resurrecting attempts
    the restore was meant to discard."""
    runbook = (DEPLOY / "README.md").read_text()
    assert "satprep.db-wal" in runbook and "satprep.db-shm" in runbook


def test_seed_transfer_keeps_each_path_in_its_own_directory():
    """Several rsync sources with one destination flatten into it, leaving the
    database where nothing looks for it."""
    runbook = (DEPLOY / "README.md").read_text()
    assert '"$SATPREP_HOST:dev/sat/data/"' in runbook
    assert '"$SATPREP_HOST:dev/sat/outputs/"' in runbook


def test_backup_unit_carries_an_absolute_uv_path():
    """systemd gives no login shell, so ~/.local/bin is off PATH and a bare
    `command -v uv` can silently skip the corpus snapshot."""
    assert "Environment=SATPREP_UV=__UV__" in (DEPLOY / "satprep-backup.service").read_text()
    body = _code(DEPLOY / "backup.sh")
    assert "SATPREP_UV" in body
    assert "corpus snapshot skipped" in body, "a skipped snapshot must say so"


def test_avahi_is_enabled_unconditionally():
    """Enablement and activity are independent; `enable --now` is idempotent
    and covers both states."""
    body = _code(DEPLOY / "install.sh")
    assert "systemctl enable --now avahi-daemon" in body
    assert "is-active --quiet avahi" not in body
    assert "is-enabled --quiet avahi" not in body


def test_installer_chowns_seeded_state():
    """install -d touches directories only; seeded files must be writable by
    the service account too."""
    body = _code(DEPLOY / "install.sh")
    assert 'chown -R "$RUN_AS:$GROUP"' in body


def test_offbox_recovery_set_includes_the_figures():
    """The archive stores images as path references, so backups need the
    figures as well as the database."""
    runbook = (DEPLOY / "README.md").read_text()
    assert "artifacts/images/" in runbook.split("## Backups", 1)[1]


def test_remote_paths_are_not_expanded_by_the_local_shell():
    """$HOME in an rsync or ssh target expands locally (for example to
    /Users/example) instead of on the remote host."""
    for path in [DEPLOY / "sync-to-server.sh", DEPLOY / "README.md"]:
        for line in _code(path).splitlines():
            if "$SATPREP_HOST:" in line or "$TARGET" in line:
                assert "$HOME" not in line, f"{path.name}: {line.strip()}"
