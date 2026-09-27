"""LAT-346: a hosted checkout's read-only mirror never produces a traceback.

The syncer owns every mode under a bound checkout's ``.lattice/``. When this
process cannot use a path there (a directory locked down by hand, a file
another user owns, a write that bypassed the syncer), a command reports
``BOARD_IS_CACHE`` (``details.reason`` ``CACHE_ACCESS``) with the way out; the
client's own directories, when this user owns them, get their 0700 back.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path

import pytest

from lattice.core.errors import BoardIsCache
from lattice.remote import session
from lattice.remote.cache_paths import cache_access_error
from tests.test_remote.hosted import (
    PROJECT,
    REMOTE,
    HostedEnv,
    chmod_writable,
    events_of,
    make_repo,
    run_cli,
)

LABEL = f"{REMOTE}/{PROJECT}"


@pytest.fixture()
def synced(hosted_env: HostedEnv, tmp_path: Path):
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", REMOTE, PROJECT).exit_code == 0
    assert run_cli(repo, "create", "First", "--actor", "agent:dev").exit_code == 0
    session.reset_process_state()
    yield repo
    chmod_writable(repo / ".lattice")


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _list(repo: Path, *extra: str):
    session.reset_process_state()
    return run_cli(repo, "list", *extra)


@pytest.mark.parametrize(
    "component", [".lattice", ".lattice/cache", ".lattice/locks", ".lattice/review_state"]
)
def test_client_directory_locked_by_hand_is_restored(synced: Path, component: str) -> None:
    (synced / component).chmod(0)
    result = _list(synced)
    assert result.exit_code == 0, result.output
    assert "First" in result.output
    assert _mode(synced / component) == 0o700


def test_locked_synced_directory_is_a_clear_error_and_cache_clear_fixes_it(
    synced: Path,
) -> None:
    tasks = synced / ".lattice" / "tasks"
    tasks.chmod(0)

    plain = _list(synced)
    assert plain.exit_code == 1
    assert plain.output.startswith(f"Error: this is a read-only mirror of {LABEL}")
    assert ".lattice/tasks (Permission denied)" in plain.output
    assert "lattice cache clear" in plain.output

    envelope = json.loads(_list(synced, "--json").output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "BOARD_IS_CACHE"
    assert envelope["error"]["message"] == plain.output.removeprefix("Error: ").rstrip("\n")

    session.reset_process_state()
    assert run_cli(synced, "cache", "clear").exit_code == 0
    result = _list(synced)
    assert result.exit_code == 0, result.output
    assert "First" in result.output


def test_directory_owned_by_someone_else_is_reported_not_changed(
    synced: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = synced / ".lattice" / "locks"
    locks.chmod(0)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

    result = _list(synced, "--json")
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "BOARD_IS_CACHE"
    assert "cannot use .lattice/locks (Permission denied)" in error["message"]
    assert _mode(locks) == 0


def test_a_write_that_bypasses_the_syncer_is_a_clear_error(
    synced: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything in a command that writes a mirror file itself meets the file's
    0400 mode; the command reports it instead of a traceback."""
    import lattice.cli.query_cmds as query_cmds

    target = next((synced / ".lattice" / "events").glob("task_*.jsonl"))
    real_load = query_cmds.load_project_config

    def load_and_write_the_mirror(lattice_dir: Path) -> dict:
        with open(target, "a", encoding="utf-8") as handle:  # 0400: PermissionError
            handle.write("{}\n")
        return real_load(lattice_dir)

    monkeypatch.setattr(query_cmds, "load_project_config", load_and_write_the_mirror)
    before = target.read_bytes()
    result = _list(synced, "--json")
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "BOARD_IS_CACHE"
    assert f"cannot use {target.relative_to(synced).as_posix()} " in error["message"]
    assert target.read_bytes() == before


class TestCacheAccessError:
    def test_absolute_path_in_a_bound_checkout(self, synced: Path) -> None:
        path = synced / ".lattice" / "events" / "x.jsonl"
        mapped = cache_access_error(PermissionError(errno.EACCES, "Permission denied", str(path)))
        assert isinstance(mapped, BoardIsCache)
        assert mapped.code == "BOARD_IS_CACHE"
        assert mapped.details == {
            "errno": "EACCES",
            "path": ".lattice/events/x.jsonl",
            "reason": "CACHE_ACCESS",
            "root": str(synced),
        }
        assert f"read-only mirror of {LABEL}" in mapped.message

    def test_local_board_is_not_mapped(self, tmp_path: Path) -> None:
        (tmp_path / ".lattice").mkdir()
        path = tmp_path / ".lattice" / "events" / "x.jsonl"
        exc = PermissionError(errno.EACCES, "Permission denied", str(path))
        assert cache_access_error(exc) is None

    def test_path_outside_the_mirror_is_not_mapped(self, synced: Path) -> None:
        exc = PermissionError(errno.EACCES, "Permission denied", str(synced / "notes.md"))
        assert cache_access_error(exc) is None

    def test_no_filename_is_not_mapped(self) -> None:
        assert cache_access_error(OSError(errno.ENOSPC, "No space left on device")) is None

    def test_the_lattice_directory_itself(self, synced: Path) -> None:
        exc = PermissionError(errno.EACCES, "Permission denied", str(synced / ".lattice"))
        mapped = cache_access_error(exc)
        assert mapped is not None and mapped.details["path"] == ".lattice"

    def test_a_wrapped_os_error_is_found(self, synced: Path) -> None:
        from lattice.storage.operations import AuthoritativeLogError

        path = synced / ".lattice" / "events" / "x.jsonl"
        try:
            try:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            except OSError as inner:
                raise AuthoritativeLogError(str(inner), path=path) from inner
        except AuthoritativeLogError as wrapped:
            mapped = cache_access_error(wrapped)
        assert mapped is not None and mapped.details["path"] == ".lattice/events/x.jsonl"

    def test_a_relative_name_is_never_placed_by_the_cwd(
        self, synced: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(synced)
        exc = PermissionError(errno.EACCES, "Permission denied", "cache_rw.lock")
        assert cache_access_error(exc) is None


def test_a_sync_failing_after_a_write_is_only_a_notice(
    synced: Path, hosted_env: HostedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server took the write; a traceback there would invite a retry that
    writes twice."""
    from lattice.boards import HostedBoard
    from lattice.remote import cache

    real_catch_up, real_record_ack = cache.catch_up, HostedBoard._record_ack
    acked: list[str] = []

    def record_ack(self, op_id, seq):  # noqa: ANN001, ANN202
        acked.append(op_id)
        return real_record_ack(self, op_id, seq)

    def catch_up_or_fail_once_acked(root: Path, **kwargs):  # noqa: ANN202
        if not acked:
            return real_catch_up(root, **kwargs)
        path = Path(root) / ".lattice" / "locks" / "cache_sync.lock"
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(HostedBoard, "_record_ack", record_ack)
    monkeypatch.setattr(cache, "catch_up", catch_up_or_fail_once_acked)
    session.reset_process_state()
    result = run_cli(synced, "comment", "DEM-1", "once", "--actor", "agent:dev")
    assert result.exit_code == 0, result.output
    assert "took the write, but the cache could not sync" in result.output
    comments = [e for e in events_of(hosted_env, "DEM-1") if e["type"] == "comment_added"]
    assert [e["data"]["body"] for e in comments] == ["once"]


class _NoFollower:
    """No stream thread: each dashboard read runs the CLI's freshness step."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def run(self) -> None:
        return None

    def stop(self) -> None:
        return None


def test_dashboard_read_of_a_locked_directory_is_an_envelope(
    synced: Path, hosted_env: HostedEnv
) -> None:
    from tests.test_dashboard.bound_helpers import dashboard, request

    task_id = json.loads((synced / ".lattice" / "ids.json").read_text())["map"]["DEM-1"]
    with dashboard(synced, follower_factory=_NoFollower, restart_after=3600) as port:
        (synced / ".lattice" / "tasks").chmod(0)
        status, payload = request(port, "GET", f"/api/tasks/{task_id}")
    assert status == 500, payload
    assert payload["ok"] is False
    assert payload["error"]["code"] == "BOARD_IS_CACHE"
    assert payload["error"]["details"]["path"] == ".lattice/tasks"
    assert "lattice cache clear" in payload["error"]["message"]


def test_mcp_tool_reports_the_cache_error(synced: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lattice.mcp.tools import LatticeToolError, lattice_list

    (synced / ".lattice" / "locks").chmod(0)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    session.reset_process_state()
    with pytest.raises(LatticeToolError) as raised:
        lattice_list(lattice_root=str(synced))
    assert raised.value.code == "BOARD_IS_CACHE"
    assert "cannot use .lattice/locks" in str(raised.value)


def test_an_op_error_no_command_renders_is_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """For example ``BINDING_CONFLICT`` from a runtime writer's layout check."""
    import click

    from lattice.cli.main import cli
    from lattice.core.errors import OpError

    @click.command("boom")
    @click.option("--json", "output_json", is_flag=True)
    def boom(output_json: bool) -> None:
        raise OpError("BINDING_CONFLICT", "not a real directory")

    monkeypatch.setitem(cli.commands, "boom", boom)
    plain = run_cli(tmp_path, "boom")
    assert plain.exit_code == 1
    assert plain.output == "Error: not a real directory\n"
    envelope = json.loads(run_cli(tmp_path, "boom", "--json").output)
    assert envelope["error"] == {"code": "BINDING_CONFLICT", "message": "not a real directory"}


# ---------------------------------------------------------------------------
# One unreadable synced file: the replay wraps the PermissionError
# (AuthoritativeLogError); every surface still reports BOARD_IS_CACHE.
# ---------------------------------------------------------------------------


def _lock_one_events_file(repo: Path) -> tuple[str, str]:
    ids = json.loads((repo / ".lattice" / "ids.json").read_text())
    task_id = ids["map"]["DEM-1"]
    events = repo / ".lattice" / "events" / f"{task_id}.jsonl"
    events.chmod(0)
    return task_id, events.relative_to(repo).as_posix()


def test_unreadable_events_file_cli(synced: Path) -> None:
    _task, rel = _lock_one_events_file(synced)
    result = _list(synced, "--json")
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "BOARD_IS_CACHE"
    assert f"cannot use {rel} (Permission denied)" in error["message"]


def test_unreadable_events_file_mcp_tool(
    synced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.mcp.tools import LatticeToolError, lattice_list

    _task, rel = _lock_one_events_file(synced)
    monkeypatch.chdir(tmp_path)  # an MCP server started elsewhere
    with pytest.raises(LatticeToolError) as raised:
        lattice_list(lattice_root=str(synced))
    assert raised.value.code == "BOARD_IS_CACHE"
    assert raised.value.details["path"] == rel


def test_unreadable_events_file_mcp_resource(
    synced: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.mcp.resources import resource_all_tasks
    from lattice.mcp.tools import LatticeToolError

    _task, rel = _lock_one_events_file(synced)
    monkeypatch.chdir(synced)
    with pytest.raises(LatticeToolError) as raised:
        resource_all_tasks()
    assert raised.value.code == "BOARD_IS_CACHE"
    assert raised.value.details["path"] == rel


def test_unreadable_events_file_dashboard_get(synced: Path, hosted_env: HostedEnv) -> None:
    from tests.test_dashboard.bound_helpers import dashboard, request

    with dashboard(synced, follower_factory=_NoFollower, restart_after=3600) as port:
        task_id, rel = _lock_one_events_file(synced)
        status, payload = request(port, "GET", f"/api/tasks/{task_id}")
    assert status == 500, payload
    assert payload["error"]["code"] == "BOARD_IS_CACHE"
    assert payload["error"]["details"]["path"] == rel


def test_dashboard_post_maps_a_cache_error_too(
    synced: Path, hosted_env: HostedEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.boards import HostedBoard
    from tests.test_dashboard.bound_helpers import dashboard, request

    task_id = json.loads((synced / ".lattice" / "ids.json").read_text())["map"]["DEM-1"]
    lock = synced / ".lattice" / "locks" / "cache_rw.lock"

    def refused(*_args, **_kwargs):  # noqa: ANN202
        raise PermissionError(errno.EACCES, "Permission denied", str(lock))

    with dashboard(synced, follower_factory=_NoFollower, restart_after=3600) as port:
        monkeypatch.setattr(HostedBoard, "execute", refused)
        status, payload = request(port, "POST", f"/api/tasks/{task_id}/comment", {"body": "hi"})
    assert status == 500, payload
    assert payload["error"]["code"] == "BOARD_IS_CACHE"
    assert payload["error"]["details"]["path"] == ".lattice/locks/cache_rw.lock"


# ---------------------------------------------------------------------------
# Descriptor-level I/O names its absolute path (never placed by the cwd).
# ---------------------------------------------------------------------------


def test_a_foreign_owned_cache_directory_fails_fchmod_clearly(
    synced: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accessible directory another user owns: ``fchmod`` gives EPERM with
    no filename at all."""
    from lattice.remote import cache_paths

    real_fchmod = os.fchmod

    def fchmod(fd: int, mode: int) -> None:
        if os.fstat(fd).st_ino == (synced / ".lattice" / "cache").stat().st_ino:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_fchmod(fd, mode)

    monkeypatch.setattr(cache_paths.os, "fchmod", fchmod)
    result = _list(synced, "--json")
    assert result.exit_code == 1
    error = json.loads(result.output)["error"]
    assert error["code"] == "BOARD_IS_CACHE"
    assert "cannot use .lattice/cache (Operation not permitted)" in error["message"]


def test_an_unwritable_lock_file_from_mcp_with_an_explicit_root(
    synced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.mcp.tools import LatticeToolError, lattice_list

    (synced / ".lattice" / "locks" / "cache_rw.lock").chmod(0)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(LatticeToolError) as raised:
        lattice_list(lattice_root=str(synced))
    assert raised.value.code == "BOARD_IS_CACHE"
    assert raised.value.details["path"] == ".lattice/locks/cache_rw.lock"


# ---------------------------------------------------------------------------
# MCP: the whole tool and resource read is covered, not only freshness.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["tool", "resource"])
def test_mcp_config_read_after_a_successful_freshness_step(
    synced: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    from lattice.mcp.resources import resource_config
    from lattice.mcp.tools import LatticeToolError, lattice_config

    fresh: list[Path] = []
    real_ensure_fresh = session.ensure_fresh

    def ensure_fresh(hosted, **kwargs):  # noqa: ANN001, ANN202
        real_ensure_fresh(hosted, **kwargs)
        fresh.append(hosted.root)  # returned: the freshness step succeeded
        (hosted.root / ".lattice" / "config.json").chmod(0)

    monkeypatch.setattr(session, "ensure_fresh", ensure_fresh)
    monkeypatch.chdir(synced)
    with pytest.raises(LatticeToolError) as raised:
        lattice_config(lattice_root=str(synced)) if surface == "tool" else resource_config()
    assert fresh
    assert raised.value.code == "BOARD_IS_CACHE"
    assert raised.value.details["path"] == ".lattice/config.json"


def _run_on_a_terminal(args: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run *args* with stderr on a pseudo-terminal (click strips ANSI sequences
    only off a terminal); returns the exit code and what the terminal got."""
    import pty
    import subprocess

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=slave,
        )
        os.close(slave)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO once the child closed its end (Linux)
                break
            if not chunk:
                break
            chunks.append(chunk)
        returncode = proc.wait(timeout=60)
    finally:
        os.close(master)
    return returncode, b"".join(chunks).decode("utf-8", "replace")


def test_an_early_cache_error_is_scrubbed_in_a_fresh_process(tmp_path: Path) -> None:
    """Classification meets the cache before anything imports the hosted
    session: a ``.lattice/`` another user owns and this one cannot enter. The
    message quotes the binding's alias, so a terminal gets it scrubbed (SPEC
    §4); ``--json`` carries the same values, escaped by JSON itself."""
    import subprocess
    import sys

    alias = "te\x1b[31mam\x07"
    repo = tmp_path / "repo"
    (repo / ".lattice").mkdir(parents=True)
    (repo / ".lattice-remote.json").write_text(json.dumps({"remote": alias, "project": PROJECT}))
    (repo / ".lattice").chmod(0)
    script = (
        "import os, sys\n"
        "os.getuid = lambda: -1  # the .lattice/ belongs to someone else\n"
        "from lattice.cli.main import cli\n"
        "assert 'lattice.remote.session' not in sys.modules\n"
        "cli(sys.argv[1:], prog_name='lattice')\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("LATTICE_ROOT", "LATTICE_DIR")}
    try:
        code, terminal = _run_on_a_terminal([sys.executable, "-c", script, "list"], repo, env)
        as_json = subprocess.run(
            [sys.executable, "-c", script, "list", "--json"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        (repo / ".lattice").chmod(0o700)
    assert code == 1, terminal
    assert "Traceback" not in terminal
    assert "\x1b" not in terminal and "\x07" not in terminal
    assert "read-only mirror of te\ufffd[31mam\ufffd/demo" in terminal
    assert "cannot use .lattice/cache (Permission denied)" in terminal
    assert as_json.returncode == 1
    error = json.loads(as_json.stdout)["error"]
    assert error["code"] == "BOARD_IS_CACHE"
    assert error["message"].startswith(f"this is a read-only mirror of {alias}/demo")
