"""``lattice doctor`` on a cache: its read-only checks, then every synced file
against the server's manifest (SPEC §9.6).

Correct answers come from the real server (``server``); forced manifest
answers and the head race from the stub (``stub``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.remote import cache
from lattice.server.testing import BoardServer
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


def test_a_clean_cache_matches(client: Path, server: BoardServer) -> None:
    create_task(server)
    cache.catch_up(client)
    result = _doctor(client)
    assert result.exit_code == 0, result.output
    assert "Cache matches the server" in result.output
    data = json.loads(_doctor(client, "--json").stdout)["data"]
    assert [f for f in data["findings"] if f["check"].startswith("cache_")] == []


def test_a_stealth_edit_is_reported(client: Path, server: BoardServer) -> None:
    task = create_task(server)
    cache.catch_up(client)
    target = client / ".lattice" / "context.md"
    original = target.read_bytes()
    _stealth_edit(target, bytes(reversed(original)))
    result = _doctor(client, "--json")
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
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(server)
    cache.catch_up(client)
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    result = _doctor(client, "--json")
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


# ---------------------------------------------------------------------------
# Hard failures are errors; only an unreachable or busy server is a warning
# ---------------------------------------------------------------------------

_JSON = {"Content-Type": "application/json", "Lattice-Protocol": "1"}


def _envelope(code: str) -> bytes:
    return json.dumps({"ok": False, "error": {"code": code, "message": code.lower()}}).encode()


@pytest.mark.parametrize(
    "answer,code",
    [
        ((200, {"Content-Type": "text/html"}, b"<html>login</html>"), "PROXY_REJECTED"),
        ((302, {"Location": "http://127.0.0.1:9/login"}, b""), "PROXY_REJECTED"),
        (
            (200, {**_JSON, "Lattice-Protocol": "2"}, b'{"ok": true, "data": {}}'),
            "PROTOCOL_MISMATCH",
        ),
        ((401, _JSON, _envelope("UNAUTHENTICATED")), "UNAUTHENTICATED"),
        ((403, _JSON, _envelope("FORBIDDEN")), "FORBIDDEN"),
        ((200, _JSON, b'{"ok": true, "data": {"epoch": "e", "files": 3}}'), "INTEGRITY_ERROR"),
    ],
)
def test_a_hard_manifest_failure_is_an_error(
    client_root: Path, stub: StubServer, answer: tuple, code: str
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    stub.fault.raw_manifest = answer
    result = _doctor(client_root, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == code
    plain = _doctor(client_root)
    assert plain.exit_code == 1
    assert "Error:" in plain.stderr


@pytest.mark.parametrize(
    "unset,code", [("LATTICE_REMOTE_TEAM_URL", "REMOTE_NOT_CONFIGURED"), (None, "TOKEN_ENV_UNSET")]
)
def test_a_remote_configuration_error_is_an_error(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch, unset, code: str
) -> None:  # noqa: ANN001
    create_task(server)
    cache.catch_up(client)
    if unset:
        monkeypatch.delenv(unset)
    else:
        monkeypatch.setenv("LATTICE_REMOTE_TEAM_HEADERS", '{"X-Proxy": "UNSET_PROXY_VAR"}')
    result = _doctor(client, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == code


@pytest.mark.parametrize(
    "answer",
    [(503, _JSON, _envelope("BOARD_BUSY")), (429, _JSON, _envelope("RATE_LIMITED"))],
)
def test_a_busy_server_is_a_warning(client_root: Path, stub: StubServer, answer: tuple) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    stub.fault.raw_manifest = answer
    result = _doctor(client_root, "--json")
    assert result.exit_code == 0, result.output
    checks = [f["check"] for f in json.loads(result.stdout)["data"]["findings"]]
    assert checks == ["cache_manifest_unavailable"]


def test_an_interrupted_cache_is_an_error_offline(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(server)
    cache.catch_up(client)
    (client / ".lattice" / "cache" / "applying").write_text(
        json.dumps({"remote": "team", "project": server.slug})
    )
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", "http://127.0.0.1:9")
    result = _doctor(client, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "CACHE_INCOMPLETE"


# ---------------------------------------------------------------------------
# The whole scan runs under the read lock (AC-9 for doctor)
# ---------------------------------------------------------------------------


def test_an_apply_waits_for_the_whole_doctor_scan(
    client: Path, server: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from lattice.storage import integrity

    task = create_task(server)
    cache.catch_up(client)
    marks: list[str] = []
    monkeypatch.setattr(cache, "_seam", marks.append)
    started: list[threading.Thread] = []
    real = integrity._collect_task_files

    def enumerate_then_race(lattice_dir: Path) -> list[Path]:
        files = real(lattice_dir)
        if not started:  # after the first enumeration, before any read
            server.op("task.archive", {"task": task})
            sync = threading.Thread(target=lambda: cache.catch_up(client, bulk=True))
            sync.start()
            started.append(sync)
            time.sleep(0.3)
            assert "applying_written" not in marks  # the apply waits for doctor
        return files

    monkeypatch.setattr(integrity, "_collect_task_files", enumerate_then_race)
    result = _doctor(client, "--json")
    started[0].join(5)
    assert result.exit_code == 0, result.output
    findings = json.loads(result.stdout)["data"]["findings"]
    assert [f for f in findings if f["level"] == "error"] == []
    assert "applying_written" in marks  # the archive applied once doctor finished
    assert (client / ".lattice" / "archive" / "tasks" / f"{task}.json").exists()


def test_a_server_integrity_error_fails_doctor(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    stub.fault.raw_manifest = (500, _JSON, _envelope("INTEGRITY_ERROR"))
    result = _doctor(client_root, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "INTEGRITY_ERROR"


def test_a_server_integrity_error_during_doctors_catch_up_fails_doctor(
    client_root: Path, stub: StubServer
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    stub.fault.raw = (500, _JSON, _envelope("INTEGRITY_ERROR"))
    result = _doctor(client_root, "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "INTEGRITY_ERROR"


def test_a_quarantined_project_is_a_warning(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    stub.fault.raw_manifest = (503, _JSON, _envelope("BOARD_UNAVAILABLE"))
    result = _doctor(client_root, "--json")
    assert result.exit_code == 0, result.output
    checks = [f["check"] for f in json.loads(result.stdout)["data"]["findings"]]
    assert checks == ["cache_manifest_unavailable"]
