"""`satprep serve`, and the one decision it makes.

The UI has no authentication. Binding it beyond loopback is therefore a
deliberate act, and the flag that does it must default to the safe side and
say something when it doesn't. These tests pin both halves.
"""

import pytest

from satprep.cli import (build_parser, cmd_serve, exposure_notice, is_loopback,
                         normalize_host)


def parse(*argv):
    return build_parser().parse_args(argv)


class FakeUvicorn:
    """Stands in for the real server so the flag can be tested without one."""

    def __init__(self):
        self.calls = []

    def run(self, app, **kwargs):
        self.calls.append((app, kwargs))


@pytest.fixture
def uvicorn(monkeypatch):
    fake = FakeUvicorn()
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake)
    return fake


# ------------------------------------------------------------- the default --

def test_serve_binds_loopback_unless_told_otherwise():
    """The default must not change silently: it is the whole reason an
    unauthenticated admin UI has been safe to run so far."""
    assert parse("serve").host == "127.0.0.1"
    assert parse("serve").port == 8765


def test_host_and_port_reach_uvicorn(uvicorn):
    cmd_serve(parse("serve", "--host", "0.0.0.0", "--port", "9000"))

    app, kwargs = uvicorn.calls[0]
    assert app == "satprep.server:app"
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000
    assert kwargs["reload"] is False


# ---------------------------------------------------------- loopback rules --

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.53", "::1", "[::1]",
                                  "localhost", "LocalHost"])
def test_loopback_forms_are_recognised(host):
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.42", "10.0.0.5",
                                  "hermes.local", "", "   "])
def test_routable_forms_are_not_mistaken_for_loopback(host):
    """0.0.0.0 is every interface, not this machine. An unresolvable name is
    assumed routable: a false warning costs a line of output, a missed one
    hides an exposed server."""
    assert not is_loopback(host)


# -------------------------------------------------------------- the notice --

def test_binding_off_machine_warns_on_stderr(uvicorn, capsys):
    cmd_serve(parse("serve", "--host", "0.0.0.0"))

    err = capsys.readouterr().err
    assert "0.0.0.0:8765" in err
    assert "no login" in err
    assert uvicorn.calls, "the warning must not stop the server from starting"


def test_the_default_bind_stays_quiet(uvicorn, capsys):
    cmd_serve(parse("serve"))
    assert capsys.readouterr().err == ""


def test_notice_names_the_address_and_what_is_exposed():
    notice = exposure_notice("192.168.1.42", 8765)
    assert "192.168.1.42:8765" in notice
    assert "/admin" in notice


# ---------------------------------------------------- bracketed IPv6 form --

def test_bracketed_ipv6_is_normalized_before_it_reaches_the_socket(uvicorn):
    """`[::1]` is URI syntax - brackets separate address from port in a URL,
    and getaddrinfo rejects them. Accepting the form in is_loopback while
    passing it through unchanged meant refusing to start on exactly the
    spelling most likely to be copied out of a browser."""
    cmd_serve(parse("serve", "--host", "[::1]"))

    assert uvicorn.calls[0][1]["host"] == "::1"


def test_normalize_host_leaves_ordinary_forms_alone():
    for host in ("127.0.0.1", "::1", "0.0.0.0", "hermes.local"):
        assert normalize_host(host) == host
    assert normalize_host("  127.0.0.1  ") == "127.0.0.1"


def test_the_notice_names_the_address_actually_bound(uvicorn, capsys):
    """A warning that quotes the un-normalized spelling sends the operator
    looking for a socket that was never opened."""
    cmd_serve(parse("serve", "--host", "[2001:db8::1]"))

    assert "2001:db8::1:8765" in capsys.readouterr().err
    assert uvicorn.calls[0][1]["host"] == "2001:db8::1"
