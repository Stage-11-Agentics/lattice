"""``lattice doctor`` on a cache: its read-only checks, then every synced file
against the server's manifest (SPEC §9.6)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.remote import cache
from tests.test_remote.conftest import create_task
from tests.test_remote.stub_sync_server import StubServer


def _doctor(client: Path, *args: str):  # noqa: ANN202
    return CliRunner().invoke(cli, ["doctor", *args], env={"LATTICE_ROOT": str(client)})


def _stealth_edit(path: Path, data: bytes) -> None:
    """Change bytes but keep size and mtime, so the fingerprint cannot see it."""
    info = path.stat()
    assert len(data) == info.st_size
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)
    path.write_bytes(data)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    os.chmod(path, 0o400)
    os.chmod(path.parent, 0o500)


def test_a_clean_cache_matches(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    result = _doctor(client_root)
    assert result.exit_code == 0, result.output
    assert "Cache matches the server" in result.output
    data = json.loads(_doctor(client_root, "--json").stdout)["data"]
    assert [f for f in data["findings"] if f["check"].startswith("cache_")] == []


def test_a_stealth_edit_is_reported(client_root: Path, stub: StubServer) -> None:
    task = create_task(stub)
    cache.catch_up(client_root)
    target = client_root / ".lattice" / "context.md"
    original = target.read_bytes()
    _stealth_edit(target, bytes(reversed(original)))
    result = _doctor(client_root, "--json")
    assert result.exit_code == 1
    findings = json.loads(result.stdout)["data"]["findings"]
    assert {
        "level": "error",
        "check": "cache_local_modification",
        "message": "context.md differs from the server's copy (modified locally).",
        "task_id": None,
    } in findings
    assert task


def test_an_unreachable_server_is_a_warning(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    result = _doctor(client_root, "--json")
    assert result.exit_code == 0, result.output
    findings = json.loads(result.stdout)["data"]["findings"]
    assert [f["check"] for f in findings if f["check"].startswith("cache_")] == [
        "cache_manifest_unavailable"
    ]


def test_the_manifest_head_is_compared_under_the_lock(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write landing between the catch-up and the manifest makes the heads
    differ; doctor retries the whole sequence and then matches."""
    create_task(stub)
    cache.catch_up(client_root)
    shots = {"left": 1}
    original = stub.sync_body

    def racing(query: dict) -> dict:
        if query.get("manifest") == "1" and shots["left"]:
            shots["left"] -= 1
            create_task(stub, "raced")
        return original(query)

    monkeypatch.setattr(stub, "sync_body", racing)
    findings = cache.manifest_findings(client_root)
    assert findings == []
    assert sum(1 for kind, q in stub.arrivals if q.get("manifest") == "1") == 2
