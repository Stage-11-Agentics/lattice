"""LAT-337: no client-side cache writer follows a symlinked or non-directory
``.lattice`` or ``cache`` (SPEC §9.4; ``lattice.remote.cache_paths``).

A clone can commit ``.lattice`` (or ``.lattice/cache``) as a symlink, and a
broken one can leave a file there. Each shape is built from a real, synced
cache, so a symlink's target is a complete cache (marker included): exactly
what a writer that followed it would write into, chmod, or delete. Every
writer, called directly and through the CLI, must refuse and leave the target
(content and mode) and the file untouched: commands with
``BINDING_CONFLICT`` (``details.reason`` ``UNSAFE_CACHE_PATH``), best-effort side
effects with their usual silent skip or one-line notice.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import acked, cache, cache_paths, session
from lattice.remote.binding import Hosted
from lattice.remote.config import resolve_remote
from lattice.remote.follower import Follower
from tests.test_remote.hosted import PROJECT, REMOTE, HostedEnv, chmod_writable, make_repo, run_cli

SHAPES = [
    pytest.param(".lattice", "symlink", id="lattice-symlink"),
    pytest.param(".lattice", "file", id="lattice-file"),
    pytest.param(".lattice/cache", "symlink", id="cache-symlink"),
    pytest.param(".lattice/cache", "file", id="cache-file"),
]


def _snapshot(path: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    """Mode, inode, mtime, and bytes of *path* and everything under it (links not
    followed), so even a same-content replacement counts as a change."""
    whole = path.is_dir() and not path.is_symlink()
    found = {}
    for entry in [path, *sorted(path.rglob("*"))] if whole else [path]:
        info = entry.lstat()
        body = entry.read_bytes() if entry.is_file() and not entry.is_symlink() else None
        found[entry.relative_to(path).as_posix()] = (
            info.st_mode,
            info.st_ino,
            info.st_mtime_ns,
            body,
        )
    return found


def _journal(env: HostedEnv) -> list[str]:
    return (env.board / "hosted" / "journal.jsonl").read_text().splitlines()


@pytest.fixture()
def synced(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", REMOTE, PROJECT).exit_code == 0
    assert run_cli(repo, "create", "First", "--actor", "agent:dev").exit_code == 0
    session.reset_process_state()
    return repo


def _make_shape(repo: Path, component: str, kind: str, outside: Path) -> list[Path]:
    """Turn *component* of a synced cache into *kind*; returns what must stay untouched."""
    path = repo / component
    chmod_writable(repo / ".lattice")
    if kind == "symlink":
        shutil.move(path, outside)  # the real cache, marker and all, now lives outside
        path.symlink_to(outside, target_is_directory=True)
        # Restrictive modes on the client's own directories out there: nothing
        # may restore (chmod) them through the link either (LAT-346).
        locked = [outside / "cache", outside / "locks"] if component == ".lattice" else [outside]
        for directory in locked:
            directory.chmod(0o500)
        return [outside, path]
    shutil.rmtree(path)
    path.write_text("not a directory\n")
    path.chmod(0o644)
    return [path]


def _hosted(repo: Path) -> Hosted:
    return Hosted(repo, REMOTE, PROJECT)


def _refused(call: Callable[[], Any]) -> None:
    with pytest.raises(OpError) as caught:
        call()
    assert caught.value.code == "BINDING_CONFLICT"
    assert caught.value.details["reason"] == cache_paths.UNSAFE_REASON


def _ledger(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    with pytest.raises(NotADirectoryError, match="not a real directory"):
        acked.record(repo / ".lattice" / "cache", op_id="op_x", project=PROJECT, epoch=None, seq=1)


def _ledger_from_a_write(repo: Path, _env: HostedEnv, capsys: Any) -> None:
    from lattice.boards import HostedBoard

    HostedBoard(_hosted(repo), repo, resolve_remote(REMOTE))._record_ack("op_x", 1)
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1 and "could not record operation op_x" in lines[0]


def _offline_window(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    hosted = _hosted(repo)
    session.open_unreachable_window(hosted)
    assert not session.in_unreachable_window(hosted)
    assert not session.window_open_at_start(hosted)
    session.open_unreachable_window(hosted)
    session.close_unreachable_window(hosted)


def _server_info(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    info = session.refresh_server_info(_hosted(repo), force=True)
    assert info["protocol"]  # fetched from the server, and not written anywhere


def _follower_record(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    notices: list[str] = []
    follower = Follower(repo, resolve_remote(REMOTE), PROJECT, on_notice=notices.append)
    follower._write(datetime.now(timezone.utc) + timedelta(seconds=30))
    assert len(notices) == 1 and "cannot write" in notices[0]


def _catch_up(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    _refused(lambda: cache.catch_up(repo, bulk=True))


def _read_lock(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    def read() -> None:
        with cache.read_lock(repo):
            pass

    _refused(read)


def _clear(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    _refused(lambda: cache.clear_cache(repo))
    _refused(lambda: cache.clear_cache(repo, forget=True))


def _apply(*, reset: bool) -> Callable[[Path, HostedEnv, Any], None]:
    """The syncer's apply called directly (a reset rescues every local file first)."""

    def run(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
        syncer = cache._Syncer(repo, resolve_remote(REMOTE), PROJECT, True, None)
        delta = cache._Delta(
            epoch="ep_x",
            head_seq=99,
            head_hash=None,
            reset=reset,
            files=[],
            removed=[],
            server_version=None,
        )
        _refused(lambda: syncer._apply(delta, {}))

    return run


def _review_state(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    from lattice.core import review

    lattice_dir = repo / ".lattice"
    _refused(lambda: review.write_review_state(lattice_dir, {"task_id": "task_x"}))
    _refused(lambda: review.clear_review_state(lattice_dir, "task_x"))
    _refused(
        lambda: review.claim_review_state(
            lattice_dir,
            "task_x",
            mode="single",
            review_type="code-review",
            started_by_pid=1,
            auto_fired=False,
        )
    )
    _refused(lambda: review.record_agent_failure(lattice_dir, "claude", "task_x"))


def _review_prompts(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    from lattice.core import agent_spawn, review

    lattice_dir = repo / ".lattice"
    _refused(lambda: review._make_prompt_dir(lattice_dir, "review-"))
    _refused(lambda: review.cleanup_prompt_dirs(lattice_dir))
    _refused(lambda: agent_spawn.make_scratch_dir(lattice_dir, "workspace"))


def _auto_review(repo: Path, _env: HostedEnv, _capsys: Any) -> None:
    from lattice.cli.auto_review import auto_fire_review

    result = auto_fire_review(
        repo / ".lattice",
        "task_x",
        "planned",
        status_event_id="ev_x",
        config={"plan_review_mode": "single"},
        no_auto_review_flag=False,
    )
    assert result == {"fired": False, "reason": "unsafe_cache_path"}


def _cli(*args: str) -> Callable[[Path, HostedEnv, Any], None]:
    def run(repo: Path, env: HostedEnv, _capsys: Any) -> None:
        before = _journal(env)
        result = run_cli(repo, *args, "--json")
        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "BINDING_CONFLICT", error
        assert _journal(env) == before  # refused before anything reached the server

    return run


WRITERS = [
    pytest.param(_ledger, id="acked.record"),
    pytest.param(_ledger_from_a_write, id="HostedBoard._record_ack"),
    pytest.param(_offline_window, id="unreachable_until"),
    pytest.param(_server_info, id="server_info.json"),
    pytest.param(_follower_record, id="follower.json"),
    pytest.param(_catch_up, id="catch_up"),
    pytest.param(_read_lock, id="read_lock"),
    pytest.param(_clear, id="clear_cache"),
    pytest.param(_apply(reset=False), id="apply-delta"),
    pytest.param(_apply(reset=True), id="apply-reset-rescue"),
    pytest.param(_review_state, id="review_state"),
    pytest.param(_review_prompts, id="tmp-prompts"),
    pytest.param(_auto_review, id="auto-review-record"),
    pytest.param(_cli("create", "Never sent", "--actor", "agent:dev"), id="cli-create"),
    pytest.param(_cli("list"), id="cli-list"),
    pytest.param(_cli("sync"), id="cli-sync"),
    pytest.param(_cli("cache", "clear"), id="cli-cache-clear"),
    pytest.param(_cli("cache", "clear", "--forget"), id="cli-cache-clear-forget"),
    pytest.param(_cli("remote", "verify"), id="cli-remote-verify"),
]


@pytest.mark.parametrize("writer", WRITERS)
@pytest.mark.parametrize(("component", "kind"), SHAPES)
def test_no_cache_writer_goes_through_a_symlink_or_a_file(
    component: str,
    kind: str,
    writer: Callable[[Path, HostedEnv, Any], None],
    hosted_env: HostedEnv,
    synced: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept = _make_shape(synced, component, kind, tmp_path / "outside")
    before = [_snapshot(path) for path in kept]
    capsys.readouterr()
    try:
        writer(synced, hosted_env, capsys)
    finally:
        session.reset_process_state()
    assert [_snapshot(path) for path in kept] == before


def test_the_refusal_names_the_path_and_the_fix(synced: Path, tmp_path: Path) -> None:
    _make_shape(synced, ".lattice", "symlink", tmp_path / "outside")
    link = synced / ".lattice"
    with pytest.raises(OpError) as caught:
        cache.catch_up(synced, bulk=True)
    assert caught.value.details == {
        "root": str(synced),
        "path": str(link),
        "reason": "UNSAFE_CACHE_PATH",
    }
    error = json.loads(run_cli(synced, "list", "--json").stdout)["error"]
    assert error["code"] == "BINDING_CONFLICT"
    assert "is a symlink" in error["message"] and f"rm {link}" in error["message"]


@pytest.mark.parametrize("runtime", cache_paths.RUNTIME_DIRS)
def test_a_symlinked_runtime_directory_is_refused_too(
    runtime: str, hosted_env: HostedEnv, synced: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside\n")
    path = synced / ".lattice" / runtime
    chmod_writable(synced / ".lattice")
    shutil.rmtree(path, ignore_errors=True)
    path.symlink_to(outside, target_is_directory=True)
    before = _snapshot(outside)
    _cli("create", "Never sent", "--actor", "agent:dev")(synced, hosted_env, None)
    _refused(lambda: cache.catch_up(synced, bulk=True))
    assert _snapshot(outside) == before


def test_a_cache_swapped_for_a_symlink_mid_apply_is_never_written_through(
    hosted_env: HostedEnv, synced: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layout is checked up front; a swap after that check (inside the apply)
    still meets the per-writer guard: state.json never lands outside."""
    hosted_env.server_op("task.create", {"title": "Second"})
    outside = tmp_path / "outside"
    outside.mkdir()
    before = _snapshot(outside)
    cache_dir = synced / ".lattice" / "cache"
    moved = tmp_path / "moved-cache"

    def swap(step: str) -> None:
        if step == "applying_written":
            shutil.move(cache_dir, moved)
            cache_dir.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(cache, "_seam", swap)
    _refused(lambda: cache.catch_up(synced, bulk=True))
    assert _snapshot(outside) == before


def test_a_normal_cache_is_unchanged_by_the_guard(hosted_env: HostedEnv, synced: Path) -> None:
    """The guard is invisible on a real cache: writes, reads, verify, clear."""
    assert run_cli(synced, "comment", "DEM-1", "hi", "--actor", "agent:dev").exit_code == 0
    listed = run_cli(synced, "list", "--json")
    assert [t["title"] for t in json.loads(listed.stdout)["data"]] == ["First"]
    data = json.loads(run_cli(synced, "remote", "verify", "--json").stdout)["data"]
    assert data["checked"] == 2 and data["missing"] == []
    lattice_dir = synced / ".lattice"
    for directory in (lattice_dir, lattice_dir / "cache", lattice_dir / "locks"):
        assert directory.stat().st_mode & 0o777 == 0o700
    assert run_cli(synced, "cache", "clear").exit_code == 0
    assert run_cli(synced, "list").exit_code == 0


@pytest.mark.parametrize(("component", "kind"), SHAPES)
def test_a_trident_pane_never_writes_its_prompt_through_the_reviewed_checkout(
    component: str,
    kind: str,
    hosted_env: HostedEnv,
    synced: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triple review hands a ``--worktree`` distinct from the task's board to the
    c11 pane, which writes its prompt under ``<worktree>/.lattice/tmp-prompts/``.
    A bound worktree whose ``.lattice`` or ``cache`` is unsafe is refused before
    any pane is created or any prompt written, from a safe board too."""
    import lattice.cli.c11_bridge as bridge
    import lattice.integrations.c11 as c11
    from lattice.core import review

    kept = _make_shape(synced, component, kind, tmp_path / "outside")
    before = [_snapshot(path) for path in kept]
    panes: list[str] = []
    monkeypatch.setenv("C11_WORKSPACE_ID", "workspace:1")
    monkeypatch.setattr(bridge, "c11_available", lambda: True)
    monkeypatch.setattr(c11, "_new_pane", lambda *a, **k: panes.append("new-pane"))

    ok, message = c11.spawn_one_in_current_workspace(
        "prompt", tab_title="t", description="d", cwd=synced
    )
    assert not ok and "a hosted checkout's cache must be a real directory" in message

    board = tmp_path / "safe-board" / ".lattice"
    board.mkdir(parents=True)
    ok, message = review.run_triple_review(
        board, "task_x", "code-review", "agent:dev", short_id="DEM-1", worktree=synced
    )
    assert not ok and "a hosted checkout's cache must be a real directory" in message
    assert panes == []
    assert not (board / "review_state").exists()
    assert [_snapshot(path) for path in kept] == before
