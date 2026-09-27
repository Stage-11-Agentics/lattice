"""``serve`` raises the soft descriptor limit toward the hard one (LAT-339).

A launchd service on macOS starts at 256 open files. ``raise_fd_limit`` asks
for ``min(hard, 65536)``, retries at OPEN_MAX (10240) when macOS refuses, and
never lowers the limit.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys

import pytest

from lattice.server import serve

INF = resource.RLIM_INFINITY


def _fake_rlimit(monkeypatch: pytest.MonkeyPatch, soft: int, hard: int, refuse_above: int | None):
    state = {"limit": (soft, hard)}

    def getrlimit(which: int) -> tuple[int, int]:
        assert which == resource.RLIMIT_NOFILE
        return state["limit"]

    def setrlimit(which: int, limits: tuple[int, int]) -> None:
        assert which == resource.RLIMIT_NOFILE
        if refuse_above is not None and limits[0] > refuse_above:
            raise ValueError("current limit exceeds maximum limit")
        state["limit"] = limits

    monkeypatch.setattr(resource, "getrlimit", getrlimit)
    monkeypatch.setattr(resource, "setrlimit", setrlimit)
    return state


def test_raises_to_the_target_when_hard_is_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_rlimit(monkeypatch, 256, INF, refuse_above=None)
    assert serve.raise_fd_limit() == {"before": 256, "soft": 65536, "hard": None}
    assert state["limit"] == (65536, INF)


def test_falls_back_to_open_max_when_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_rlimit(monkeypatch, 256, INF, refuse_above=10240)
    assert serve.raise_fd_limit() == {"before": 256, "soft": 10240, "hard": None}
    assert state["limit"] == (10240, INF)


def test_capped_at_the_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_rlimit(monkeypatch, 256, 4096, refuse_above=None)
    assert serve.raise_fd_limit() == {"before": 256, "soft": 4096, "hard": 4096}
    assert state["limit"] == (4096, 4096)


def test_never_lowers(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_rlimit(monkeypatch, 1048576, INF, refuse_above=None)
    assert serve.raise_fd_limit() == {"before": 1048576, "soft": 1048576, "hard": None}
    assert state["limit"] == (1048576, INF)


def test_keeps_the_limit_when_every_raise_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _fake_rlimit(monkeypatch, 256, INF, refuse_above=256)
    assert serve.raise_fd_limit() == {"before": 256, "soft": 256, "hard": None}
    assert state["limit"] == (256, INF)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor limits")
def test_real_process_at_256_is_raised() -> None:
    """In a child started at the stock macOS limit, the real syscall takes the raise."""
    code = (
        "import json, resource\n"
        "_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))\n"
        "from lattice.server.serve import raise_fd_limit\n"
        "result = raise_fd_limit()\n"
        "result['actual'] = resource.getrlimit(resource.RLIMIT_NOFILE)[0]\n"
        "print(json.dumps(result))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    ).stdout
    result = json.loads(out)
    assert result["before"] == 256
    assert result["soft"] == result["actual"]
    hard = result["hard"]
    assert result["soft"] >= min(hard or 10240, 10240)
