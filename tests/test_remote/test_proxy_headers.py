"""AC-20 (client side): proxy headers, redirects on info/op/op-status,
``INSECURE_URL``, and the first-contact errors (SPEC §9.1)."""

from __future__ import annotations

import http.client
import json
import os
import stat
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import client
from lattice.remote.config import resolve_remote
from tests.test_remote.hosted import TOKEN_ENV, HostedEnv, make_repo, run_cli
from tests.test_remote.proxies import fixed_answer, recording_listener

PROXY_ID = "proxy-id-7f3a"
PROXY_SECRET = "proxy-secret-19c2"
OP_ID = "op_01J9Z0000000000000000000AB"


@contextmanager
def header_proxy(target: str) -> Iterator[dict[str, Any]]:
    """A reverse proxy that answers 403 (an HTML page) unless both
    ``X-Proxy-Id`` and ``X-Proxy-Secret`` carry the expected values."""
    seen: dict[str, Any] = {"rejected": 0, "forwarded": 0}
    upstream = urllib.parse.urlsplit(target)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _handle(self) -> None:
            if (
                self.headers.get("X-Proxy-Id") != PROXY_ID
                or self.headers.get("X-Proxy-Secret") != PROXY_SECRET
            ):
                seen["rejected"] += 1
                body = b"<html>Access denied</html>"
                self.send_response(403)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            seen["forwarded"] += 1
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=30)
            headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            payload = response.read()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            conn.close()

        do_GET = do_POST = _handle

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    seen["url"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _all_bytes(*roots: Path) -> bytes:
    chunks = []
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            if "/.git" in dirpath:
                continue
            for name in filenames:
                chunks.append((Path(dirpath) / name).read_bytes())
    return b"\n".join(chunks)


PROXY_HEADERS = {
    "X-Proxy-Id": {"env": "PROXY_ID_VAR"},
    "X-Proxy-Secret": {"env": "PROXY_SECRET_VAR"},
}


def test_client_passes_the_proxy_with_its_headers(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROXY_ID_VAR", PROXY_ID)
    monkeypatch.setenv("PROXY_SECRET_VAR", PROXY_SECRET)
    with header_proxy(hosted_env.url) as proxy:
        hosted_env.write_remote(url=proxy["url"], headers=PROXY_HEADERS)
        repo = make_repo(tmp_path / "repo")
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
        created = run_cli(repo, "create", "Through the proxy", "--actor", "human:alice")
        assert created.exit_code == 0, created.output
        shown = run_cli(repo, "show", "DEM-1", "--json")
        assert json.loads(shown.stdout)["data"]["title"] == "Through the proxy"
        assert proxy["forwarded"] >= 3 and proxy["rejected"] == 0
    written = _all_bytes(repo, tmp_path / "config")
    for secret in (PROXY_ID, PROXY_SECRET, hosted_env.token):
        assert secret.encode() not in written


def test_missing_proxy_headers_are_proxy_rejected(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    with header_proxy(hosted_env.url) as proxy:
        hosted_env.write_remote(url=proxy["url"])
        for args in (("create", "x", "--actor", "human:alice", "--json"), ("list", "--json")):
            result = run_cli(repo, *args)
            assert result.exit_code == 1, result.output
            error = json.loads(result.stdout)["error"]
            assert error["code"] == "PROXY_REJECTED"
            assert "HTTP 403" in error["message"]
        # A header whose variable is unset is TOKEN_ENV_UNSET, naming it.
        hosted_env.write_remote(headers=PROXY_HEADERS)
        monkeypatch.setenv("PROXY_ID_VAR", PROXY_ID)
        result = run_cli(repo, "list", "--json")
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "TOKEN_ENV_UNSET"
        assert "PROXY_SECRET_VAR" in error["message"]


def test_redirects_on_info_op_and_op_status_carry_no_credentials(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROXY_ID_VAR", PROXY_ID)
    monkeypatch.setenv("PROXY_SECRET_VAR", PROXY_SECRET)
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    with recording_listener() as second:
        location = {"Location": second.url + "/login"}
        with fixed_answer(302, location) as redirector:
            hosted_env.write_remote(url=redirector.url, headers=PROXY_HEADERS)
            remote = resolve_remote("team")
            calls = (
                lambda: client.get_json(remote, "/v1/info"),
                lambda: client.post_operation(
                    remote, "demo", "task.create", {"op_id": OP_ID, "params": {"title": "x"}}
                ),
                lambda: client.op_status(remote, "demo", OP_ID),
            )
            for call in calls:
                with pytest.raises(OpError) as exc:
                    call()
                assert exc.value.code == "PROXY_REJECTED"
                assert "302" in exc.value.message
            for args in (
                ("create", "x", "--actor", "human:alice", "--json"),
                ("remote", "op-status", OP_ID, "--json"),
            ):
                result = run_cli(repo, *args)
                assert result.exit_code == 1, result.output
                assert json.loads(result.stdout)["error"]["code"] == "PROXY_REJECTED"
            assert len(redirector.requests) >= 5
    assert second.requests == []


def test_insecure_url_is_refused_unless_plaintext_is_allowed(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add = run_cli(tmp_path, "remote", "add", "far", "http://lattice.example.com", "--json")
    assert add.exit_code == 1
    assert json.loads(add.stdout)["error"]["code"] == "INSECURE_URL"
    allowed = run_cli(
        tmp_path,
        "remote",
        "add",
        "far",
        "http://lattice.example.com",
        "--token-env",
        "FAR_TOKEN",
        "--allow-plaintext",
    )
    assert allowed.exit_code == 0, allowed.output
    monkeypatch.setenv("FAR_TOKEN", "t")
    assert resolve_remote("far").allow_plaintext is True
    loopback = run_cli(tmp_path, "remote", "add", "near", "http://127.0.0.5:9", "--json")
    assert loopback.exit_code == 0, loopback.output

    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    hosted_env.write_remote(url="http://lattice.example.com:9")
    result = run_cli(repo, "list", "--json")
    assert json.loads(result.stdout)["error"]["code"] == "INSECURE_URL"
    result = run_cli(repo, "create", "x", "--actor", "human:alice", "--json")
    assert json.loads(result.stdout)["error"]["code"] == "INSECURE_URL"
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_ALLOW_PLAINTEXT", "1")
    assert resolve_remote("team").allow_plaintext is True


def test_unconfigured_alias_and_unset_token(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".lattice-remote.json").write_text(
        json.dumps({"remote": "nowhere", "project": "demo"})
    )
    for args in (("list", "--json"), ("create", "x", "--actor", "agent:a", "--json")):
        result = run_cli(repo, *args)
        assert result.exit_code == 1
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "REMOTE_NOT_CONFIGURED"
        assert "lattice remote add nowhere <url> --token-env <VAR>" in error["message"]
        assert "ask your server admin" in error["message"]
    plain = run_cli(repo, "list")
    assert "lattice remote add nowhere <url> --token-env <VAR>" in plain.stderr

    hosted_env.bind(repo)
    monkeypatch.delenv(TOKEN_ENV)
    result = run_cli(repo, "list", "--json")
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "TOKEN_ENV_UNSET"
    assert TOKEN_ENV in error["message"]


def test_remote_add_and_list_never_show_a_token(tmp_path: Path) -> None:
    added = run_cli(
        tmp_path,
        "remote",
        "add",
        "team",
        "https://lattice.example.com/",
        "--token-stdin",
        "--header",
        "X-Proxy-Id=PROXY_ID_VAR",
        input="literal-token-value\n",
    )
    assert added.exit_code == 0, added.output
    assert "literal-token-value" not in added.output
    path = tmp_path / "config" / "lattice" / "remotes.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    entry = json.loads(path.read_text())["remotes"]["team"]
    assert entry["url"] == "https://lattice.example.com"
    assert entry["headers"] == {"X-Proxy-Id": {"env": "PROXY_ID_VAR"}}
    listed = run_cli(tmp_path, "remote", "list")
    assert "team\thttps://lattice.example.com" in listed.stdout
    listed_json = run_cli(tmp_path, "remote", "list", "--json")
    assert "literal-token-value" not in listed_json.stdout
    bad = run_cli(tmp_path, "remote", "add", "x", "https://h", "--header", "nope", "--json")
    assert json.loads(bad.stdout)["error"]["code"] == "VALIDATION_ERROR"


@pytest.fixture(autouse=True)
def _private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
