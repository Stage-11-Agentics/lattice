"""No bearer token or proxy header value in the repr or str of a remote, or of
any client object that holds one (G-7: no token secret in any log)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import HostedBoard
from lattice.remote import cache
from lattice.remote.binding import Hosted
from lattice.remote.config import resolve_remote
from lattice.remote.http import Remote

TOKEN = "tok-secret-4f2a9c"
HEADER = "hdr-secret-77b1e0"


def _texts(obj: object) -> list[str]:
    return [repr(obj), str(obj)]


def _assert_clean(obj: object) -> None:
    for text in _texts(obj):
        assert TOKEN not in text, text
        assert HEADER not in text, text


def test_a_remote_hides_its_credentials() -> None:
    remote = Remote(
        alias="team", url="https://h.example.com", token=TOKEN, headers={"X-P": HEADER}
    )
    _assert_clean(remote)
    assert "team" in repr(remote)  # still useful in a log


def test_resolved_remote_and_its_holders_hide_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "https://h.example.com")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", TOKEN)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_HEADERS", '{"X-P": "PROXY_VAR"}')
    monkeypatch.setenv("PROXY_VAR", HEADER)
    remote = resolve_remote("team")
    assert remote.token == TOKEN and remote.headers == {"X-P": HEADER}
    _assert_clean(remote)
    hosted = Hosted(tmp_path, "team", "demo")
    _assert_clean(HostedBoard(hosted, tmp_path, remote))
    _assert_clean(cache._Syncer(tmp_path, remote, "demo", bulk=True, deadline=None))
