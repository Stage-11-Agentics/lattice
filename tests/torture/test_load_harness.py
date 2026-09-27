"""The AC-42 harness never loses a child (H-13b round 4): a child that reports
during the run, or exits without a report, fails the test at once. Default
suite: these use short sleeps and a short flush grace."""

from __future__ import annotations

import multiprocessing
import time

import pytest

from tests.torture.test_load import _collect, _run_for


def _report_error(out) -> None:
    out.put((7, "viewer", 0, 0, ['OSError(49, "Can\'t assign requested address")']))


def _silent_death() -> None:
    import os

    os._exit(3)


def _sleep() -> None:
    time.sleep(30)


@pytest.fixture()
def ctx():
    return multiprocessing.get_context("spawn")


def test_a_child_that_exits_without_a_report_fails_the_run_fast(ctx) -> None:
    stop, results = ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_silent_death, daemon=True)
    child.start()
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=r"exited \(3\) without a report"):
        _run_for(30, results, {4: child}, stop, grace=0.3)
    assert time.monotonic() - started < 5
    assert stop.is_set()


def test_a_child_reporting_during_the_run_fails_it_with_the_error(ctx) -> None:
    stop, results = ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_report_error, args=(results,), daemon=True)
    child.start()
    with pytest.raises(pytest.fail.Exception, match="assign requested address"):
        _run_for(30, results, {7: child}, stop, grace=0.3)
    child.join(timeout=5)


def test_collect_fails_fast_on_a_silent_death(ctx) -> None:
    stop, results = ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_silent_death, daemon=True)
    child.start()
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match="without a report"):
        _collect(results, {1: child}, stop, until=time.monotonic() + 60, grace=0.3)
    assert time.monotonic() - started < 5


def test_a_living_child_does_not_fail_the_run(ctx) -> None:
    stop, results = ctx.Event(), ctx.Queue()
    child = ctx.Process(target=_sleep, daemon=True)
    child.start()
    try:
        _run_for(0.5, results, {2: child}, stop, grace=0.3)
    finally:
        child.kill()
        child.join(timeout=5)


def _ready_then_die(ready, delay: float) -> None:
    import os

    ready.set()
    time.sleep(delay)
    os._exit(4)


def test_a_death_just_before_the_deadline_is_not_dropped(ctx) -> None:
    """Round 4 finding 2: a silent death seen less than the grace before the run
    ends still fails the run."""
    stop, results, ready = ctx.Event(), ctx.Queue(), ctx.Event()
    child = ctx.Process(target=_ready_then_die, args=(ready, 0.6), daemon=True)
    child.start()
    assert ready.wait(10)
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=r"exited \(4\) without a report"):
        _run_for(0.75, results, {5: child}, stop, grace=0.5)
    assert time.monotonic() - started < 3


# ---------------------------------------------------------------------------
# The clients' separate filesystem (EVALUATION AC-42, PR #90): fails, never skips
# ---------------------------------------------------------------------------


def test_the_device_policy_refuses_one_filesystem_and_accepts_two(tmp_path) -> None:
    from tests.torture.client_fs import assert_separate_filesystems

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    with pytest.raises(AssertionError, match="are on one filesystem"):
        assert_separate_filesystems(a, b, device=lambda _p: 7)
    assert_separate_filesystems(a, b, device=lambda p: 1 if p == a else 2)


def test_the_client_directory_is_removed_however_the_block_ends(tmp_path) -> None:
    from contextlib import contextmanager

    from tests.torture.client_fs import separate_client_filesystem

    @contextmanager
    def fake_mount():
        yield tmp_path

    with pytest.raises(RuntimeError), separate_client_filesystem(fake_mount) as path:
        (path / "mirror").mkdir()
        raise RuntimeError("the run failed")
    assert list(tmp_path.iterdir()) == []


def test_no_separate_filesystem_is_a_failure_not_a_skip(monkeypatch) -> None:
    import sys

    from tests.torture import client_fs

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(client_fs.os, "access", lambda *_a: False)
    with pytest.raises(AssertionError, match="/dev/shm is not a writable directory"):
        with client_fs.platform_mount():
            pass
