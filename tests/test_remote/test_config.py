"""The read-only remote resolver (SPEC §9.1, resolution D1)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.remote.config import remotes_path, resolve_remote


def _write(remotes: dict, mode: int = 0o600) -> Path:
    path = remotes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"remotes": remotes}))
    os.chmod(path, mode)
    return path


@pytest.fixture(autouse=True)
def _config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def test_file_remote_with_env_token_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _write(
        {
            "team": {
                "url": "https://lattice.example.internal/",
                "token": {"env": "TEAM_TOKEN"},
                "headers": {"CF-Access-Client-Id": {"env": "PROXY_ID"}},
            }
        }
    )
    monkeypatch.setenv("TEAM_TOKEN", "t0k")
    monkeypatch.setenv("PROXY_ID", "pid")
    remote = resolve_remote("team")
    assert remote.url == "https://lattice.example.internal"
    assert remote.token == "t0k"
    assert dict(remote.headers) == {"CF-Access-Client-Id": "pid"}


def test_a_literal_token_in_a_private_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _write({"team": {"url": "http://127.0.0.1:1", "token": "literal"}})
    assert resolve_remote("team").token == "literal"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_file_other_users_can_read_is_refused_before_it_is_read(mode: int) -> None:
    path = _write({"team": {"url": "http://127.0.0.1:1", "token": "literal"}}, mode)
    with pytest.raises(OpError) as err:
        resolve_remote("team")
    assert err.value.code == "VALIDATION_ERROR"
    assert f"chmod 600 {path}" in err.value.message


def test_the_check_uses_the_opened_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode is judged on the descriptor that is read, not on a separate stat."""
    path = _write({"team": {"url": "http://127.0.0.1:1", "token": "literal"}}, 0o644)
    real_fstat = os.fstat
    calls: list[int] = []

    def fstat(fd: int) -> os.stat_result:
        calls.append(fd)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(OpError) as err:
        resolve_remote("team")
    assert calls and str(path) in err.value.message


def test_a_symlinked_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"remotes": {"team": {"url": "http://127.0.0.1:1"}}}))
    os.chmod(real, 0o600)
    path = remotes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(real)
    with pytest.raises(OpError) as err:
        resolve_remote("team")
    assert err.value.code == "VALIDATION_ERROR"


def test_the_environment_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _write({"my-team": {"url": "http://file.invalid", "token": "from-file"}})
    monkeypatch.setenv("LATTICE_REMOTE_MY_TEAM_URL", "http://127.0.0.1:7")
    monkeypatch.setenv("LATTICE_REMOTE_MY_TEAM_TOKEN", "from-env")
    monkeypatch.setenv("LATTICE_REMOTE_MY_TEAM_HEADERS", '{"X-Proxy": "PROXY_VAR"}')
    monkeypatch.setenv("PROXY_VAR", "pv")
    remote = resolve_remote("my-team")
    assert (remote.url, remote.token, dict(remote.headers)) == (
        "http://127.0.0.1:7",
        "from-env",
        {"X-Proxy": "pv"},
    )


def test_environment_only_with_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:7")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", "t")
    assert resolve_remote("team").token == "t"


def test_remote_not_configured() -> None:
    with pytest.raises(OpError) as err:
        resolve_remote("team")
    assert err.value.code == "REMOTE_NOT_CONFIGURED"
    assert err.value.message == (
        "no remote named 'team' is configured on this machine. Run: lattice remote add "
        "team <url> --token-env <VAR> (ask your server admin for the URL and a token)."
    )


@pytest.mark.parametrize("unset", ["TEAM_TOKEN", "PROXY_ID"])
def test_token_env_unset_names_the_variable(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    _write(
        {
            "team": {
                "url": "http://127.0.0.1:1",
                "token": {"env": "TEAM_TOKEN"},
                "headers": {"X-Id": {"env": "PROXY_ID"}},
            }
        }
    )
    for var in ("TEAM_TOKEN", "PROXY_ID"):
        monkeypatch.setenv(var, "set")
    monkeypatch.setenv(unset, "")
    with pytest.raises(OpError) as err:
        resolve_remote("team")
    assert err.value.code == "TOKEN_ENV_UNSET"
    assert unset in err.value.message
    assert err.value.details == {"remote": "team", "variable": unset}
