"""Filing-only token mint flags and their persisted record."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli


def test_server_token_create_persists_filing_contract(tmp_path: Path) -> None:
    root = tmp_path / "server-root"
    runner = CliRunner()
    initialized = runner.invoke(cli, ["server", "init", "--root", str(root), "--json"])
    assert initialized.exit_code == 0, initialized.output
    project = runner.invoke(
        cli,
        ["server", "project", "create", "alpha", "--root", str(root), "--json"],
    )
    assert project.exit_code == 0, project.output

    result = runner.invoke(
        cli,
        [
            "server",
            "token",
            "create",
            "--user",
            "human:alice",
            "--machine",
            "reporter-ingest",
            "--actor",
            "agent:intake",
            "--project",
            "alpha",
            "--only",
            "issue.file",
            "--source",
            " reporter-mail ",
            "--ops-per-minute",
            "7",
            "--bytes-per-minute",
            str(128 * 1024 * 1024),
            "--max-staged-bytes",
            "16384",
            "--root",
            str(root),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)["data"]["record"]
    assert record["only"] == ["issue.file"]
    assert record["source"] == "reporter-mail"
    assert record["ops_per_minute"] == 7
    assert record["bytes_per_minute"] == 128 * 1024 * 1024
    assert record["max_staged_bytes"] == 16384

    listed = runner.invoke(cli, ["server", "token", "list", "--root", str(root)])
    assert listed.exit_code == 0, listed.output
    assert "ops_per_minute: 7" in listed.output
    assert f"bytes_per_minute: {128 * 1024 * 1024}" in listed.output
    assert "max_staged_bytes: 16384" in listed.output


def test_server_token_create_allows_limit_overrides_without_restriction(tmp_path: Path) -> None:
    root = tmp_path / "server-root"
    runner = CliRunner()
    initialized = runner.invoke(cli, ["server", "init", "--root", str(root), "--json"])
    assert initialized.exit_code == 0, initialized.output
    project = runner.invoke(
        cli,
        ["server", "project", "create", "alpha", "--root", str(root), "--json"],
    )
    assert project.exit_code == 0, project.output

    result = runner.invoke(
        cli,
        [
            "server",
            "token",
            "create",
            "--user",
            "human:alice",
            "--machine",
            "trusted-service",
            "--actor",
            "agent:service",
            "--project",
            "alpha",
            "--ops-per-minute",
            "91",
            "--bytes-per-minute",
            str(128 * 1024 * 1024),
            "--max-staged-bytes",
            "262144",
            "--root",
            str(root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)["data"]["record"]
    assert record.get("only", []) == []
    assert record.get("source") is None
    assert record["ops_per_minute"] == 91
    assert record["bytes_per_minute"] == 128 * 1024 * 1024
    assert record["max_staged_bytes"] == 262144
