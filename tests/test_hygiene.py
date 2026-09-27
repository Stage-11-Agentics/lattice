"""G-3 / AC-33: the repository carries no deployment-specific hostnames, tokens, or secrets.

The rules live in ``tests/hygiene.py``. Samples below are assembled at run time
so this file never contains a string its own scan would flag.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests import hygiene
from tests.hygiene import Denylist, load_denylist, scan_text

# Read at import, before any fixture strips LATTICE_* from the environment.
_PRIVATE_DENYLIST = os.environ.get(hygiene.PRIVATE_DENYLIST_ENV)


def _rules(text: str) -> list[str]:
    return [f.rule for f in scan_text("sample.txt", text)]


TS = ".ts" + ".net"
CFA = ".cloudflare" + "access.com"
HEX32 = "0123456789abcdef" * 2
HEX64 = HEX32 * 2


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("token = lat_tok_" + "Q" * 24, "lattice-token"),
        ("-----BEGIN " + "OPENSSH PRIVATE KEY-----", "private-key"),
        ("-----BEGIN " + "PRIVATE KEY-----", "private-key"),
        (f"CF-Access-Client-Id: {HEX32}.access", "cf-access-client-id"),
        ("CF-Access-Client-Secret: " + HEX64, "cf-access-client-secret"),
        ('"cf_access_client_secret": "' + HEX64 + '"', "cf-access-client-secret"),
        ("Cf-Access-Jwt-Assertion: eyJ" + "a" * 10 + ".b1.c2", "cf-access-jwt"),
        ("https://atlas.tail1234" + TS + "/v1/info", "tailnet-name"),
        ("tail1234" + TS, "tailnet-name"),
        ("login at myteam" + CFA, "cloudflare-access-team"),
        ("server at " + "100.64" + ".0.1:8740", "tailnet-ipv4"),
        ("peer " + "100.127" + ".255.255", "tailnet-ipv4"),
        ("route " + "100.101" + ".7.9/32", "tailnet-ipv4"),
        ("host " + "100.64" + ".1.42/16", "tailnet-ipv4"),
        ("net " + "100.64" + ".0.0/16", "tailnet-ipv4"),
        ("net " + "100.64" + ".0.0/9", "tailnet-ipv4"),
        ("team at example" + CFA, "cloudflare-access-team"),
    ],
)
def test_builtin_rule_fires(text: str, rule: str) -> None:
    assert rule in _rules(text)


@pytest.mark.parametrize(
    "text",
    [
        "tokens look like `lat_tok_...` and are shown once",
        "lat_tok_<secret>",
        "BEGIN PUBLIC KEY",
        '{"CF-Access-Client-Id": "CF_ACCESS_CLIENT_ID"}',
        "https://atlas.example" + TS + "/v1/info",
        "example" + TS,
        "`*" + TS + "` names",
        "<team>" + CFA,
        "100.63" + ".255.255 is outside the range",
        "100.128" + ".0.1 is outside the range",
        "version 1.100.64" + ".1.2",
        "at 100.64" + ".0.1.5 (not an address)",
        "the tailnet range " + "100.64" + ".0.0/10",
    ],
)
def test_placeholder_does_not_fire(text: str) -> None:
    assert _rules(text) == []


def test_salted_denylist_matches_tokens_and_subdomains(tmp_path: Path) -> None:
    path = tmp_path / "deny.sha256"
    hygiene.add_to_denylist("secret-host.internal", path)
    hygiene.add_to_denylist("secret-host.internal", path)  # idempotent
    denylist = load_denylist(path)
    assert denylist.salt and len(denylist.entries) == 1
    assert "secret-host" not in path.read_text()

    def hits(text: str) -> bool:
        return any(f.rule == "denylisted string" for f in scan_text("f", text, [denylist]))

    assert hits("curl https://Secret-Host.Internal/healthz")
    assert hits("go to api.secret-host.internal.")
    assert not hits("secret-host.internal2 and other-secret-host.internal-ish")
    assert not hits("secret host internal")


def test_plain_denylist(tmp_path: Path) -> None:
    path = tmp_path / "private.txt"
    path.write_text("# private\nMy-Box.lan\n")
    denylist = load_denylist(path)
    assert denylist == Denylist(None, frozenset({"my-box.lan"}))
    assert [f.rule for f in scan_text("f", "ssh my-box.lan", [denylist])] == ["denylisted string"]


def test_findings_never_include_the_matched_text() -> None:
    finding = scan_text("docs/x.md", "x lat_tok_" + "Z" * 30)[0]
    assert str(finding) == "docs/x.md:1: lattice-token"


def test_committed_denylist_is_salted() -> None:
    if not hygiene.DENYLIST_PATH.exists():
        pytest.skip("no committed denylist")
    denylist = load_denylist(hygiene.DENYLIST_PATH)
    assert denylist.salt
    assert all(len(e) == 64 and int(e, 16) >= 0 for e in denylist.entries)


def test_tracked_files_are_clean() -> None:
    """The check itself: built-in rules plus the committed denylist over tracked files."""
    if hygiene.tracked_files() is None:
        pytest.skip("not a git work tree (e.g. an sdist); the check needs git ls-files")
    denylists = hygiene.active_denylists({})
    findings = hygiene.scan_repo(denylists=denylists)
    assert not findings, "hygiene findings (G-3):\n" + "\n".join(map(str, findings))


@pytest.mark.skipif(not _PRIVATE_DENYLIST, reason="LATTICE_HYGIENE_DENYLIST not set")
def test_tracked_files_clear_the_private_denylist() -> None:
    private = [load_denylist(Path(_PRIVATE_DENYLIST).expanduser())]  # type: ignore[arg-type]
    findings = hygiene.scan_repo(denylists=private)
    assert not findings, "private denylist findings:\n" + "\n".join(map(str, findings))
