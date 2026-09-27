"""AC-14, G-7: secrets never at rest in plaintext and never in a log."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import tokens
from lattice.server.testing import running_server


def test_token_secret_is_printed_once_and_stored_hashed(root: Path) -> None:
    runner = CliRunner()
    created = runner.invoke(
        cli,
        [
            "server",
            "token",
            "create",
            "--user",
            "human:alice",
            "--machine",
            "m",
            "--all-projects",
            "--root",
            str(root),
        ],
    )
    assert created.exit_code == 0
    token = created.output.splitlines()[0]
    token_id, secret = tokens.parse_token(token)
    stored = (root / "tokens.json").read_text()
    assert secret not in stored and token not in stored
    assert tokens.hash_secret(secret) in stored
    for args in (["--json"], []):
        listed = runner.invoke(cli, ["server", "token", "list", "--root", str(root), *args])
        assert token_id in listed.output
        assert secret not in listed.output and "sha256" not in listed.output
    as_json = runner.invoke(
        cli,
        [
            "server",
            "token",
            "create",
            "--user",
            "human:b",
            "--machine",
            "m",
            "--json",
            "--root",
            str(root),
        ],
    )
    payload = json.loads(as_json.output)["data"]
    assert payload["token"].startswith("lat_tok_") and "sha256" not in payload["record"]


def test_server_logs_never_hold_a_secret(root: Path) -> None:
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    token = data["token"]
    _, secret = tokens.parse_token(token)
    with running_server(root) as server:
        server.request("GET", "/v1/info", token=token)
        server.op("alpha", "task.create", {"title": "hello"}, token=token)
        server.op("alpha", "task.create", {"title": "x"}, token=token + "x")
        server.request("GET", "/v1/info", headers={"Authorization": token})
        server.op("alpha", "nope.op", {"title": token}, token=token)
        tokens.revoke_token(root, data["record"]["id"])
        server.request("GET", "/v1/info", token=token)
        log_text = server.log_stream.getvalue()
    assert log_text
    assert secret not in log_text and token not in log_text
    assert '"config_reload"' in log_text


def test_token_parsing_handles_underscores_and_hyphens_in_secrets() -> None:
    token_id = tokens.new_token_id()
    for secret in ("a_b-c" + "x" * 38, "_" * 43, "-" * 43, "A-_" * 14 + "z"):
        assert tokens.parse_token(tokens.format_token(token_id, secret)) == (token_id, secret)
    assert tokens.parse_token(f"lat_{token_id}_short") is None
    assert tokens.parse_token(f"lat_{token_id}-" + "x" * 43) is None
    for _ in range(50):
        secret = tokens.new_secret()
        assert tokens.parse_token(tokens.format_token(token_id, secret)) == (token_id, secret)


def test_a_crash_carrying_a_secret_never_reaches_the_log(root: Path) -> None:
    """A1: crash logs hold exception types and stack frames, never messages or values."""
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    token = data["token"]
    _, secret = tokens.parse_token(token)
    raw_secret = tokens.new_secret()  # a bare 43-character secret, no lat_ prefix
    with running_server(root) as server:
        for text in (raw_secret, token, f"payload-{raw_secret}-tail"):
            status, _, body = server.op("alpha", "xtest.leak", {"text": text}, token=token)
            assert status == 500 and body["error"]["code"] == "INTERNAL_ERROR"
            assert raw_secret not in json.dumps(body)
        log_text = server.log_stream.getvalue()
        crashes = [x for x in server.log_lines if x["event"] == "op_crashed"]
    assert len(crashes) == 3
    assert crashes[0]["exception"] == "RuntimeError"
    assert crashes[0]["chain"] == ["RuntimeError", "ValueError"]
    assert any("server_ops.py" in f for f in crashes[0]["frames"])
    for leaked in (raw_secret, secret, token):
        assert leaked not in log_text


def test_web_sessions_hold_no_secret_and_logs_hold_no_cookie(root: Path) -> None:
    """G-7 (H-13b): web_sessions.json stores hashes only, mode 0600; neither the
    token submitted at login nor any session cookie value reaches the log."""
    import os
    import stat

    from tests.test_server.web_client import WebClient

    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    token = data["token"]
    _, secret = tokens.parse_token(token)
    with running_server(root) as server:
        web = WebClient(server)
        web.login(token + "x")  # a failed login
        web.login(token, origin="http://evil.example")  # a refused login
        web.login(token)
        cookie = web.session
        assert cookie
        web.get("/p/alpha/api/tasks")
        web.post_json("/p/alpha/api/tasks", {"title": "hello"})
        web.get("/")
        web.logout()
        web.cookies["lattice_session"] = cookie
        web.get("/p/alpha/api/tasks")  # a dead session
        log_text = server.log_stream.getvalue()
    stored = (root / "web_sessions.json").read_text()
    for value in (token, secret, cookie):
        assert value not in log_text
        assert value not in stored
    assert '"/login"' in log_text  # the requests were logged, by path only
    mode = stat.S_IMODE(os.stat(root / "web_sessions.json").st_mode)
    assert mode == 0o600
