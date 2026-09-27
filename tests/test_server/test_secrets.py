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
