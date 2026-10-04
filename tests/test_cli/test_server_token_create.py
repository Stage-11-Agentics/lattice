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
            "8192",
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
    assert record["bytes_per_minute"] == 8192
    assert record["max_staged_bytes"] == 16384
