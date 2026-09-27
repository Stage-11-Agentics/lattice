"""Per-token limits and the disk floor, with a fake clock (SPEC §8.1, §8.11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.server.config import Limits
from lattice.server.limits import DiskFloor, TokenLimits


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_inflight() -> None:
    limits = TokenLimits(Limits(max_inflight_per_token=2), Clock())
    limits.enter("a")
    limits.enter("a")
    with pytest.raises(OpError) as exc:
        limits.enter("a")
    assert exc.value.code == "RATE_LIMITED" and exc.value.details["retry_after"] == 1
    limits.enter("b")
    limits.leave("a")
    limits.enter("a")


def test_op_bucket_refills_continuously() -> None:
    clock = Clock()
    limits = TokenLimits(Limits(token_ops_per_minute=60), clock)
    for _ in range(60):
        limits.take_op("a")
    with pytest.raises(OpError) as exc:
        limits.take_op("a")
    assert exc.value.details["retry_after"] == 1
    limits.take_op("b")
    clock.now += 1.0
    limits.take_op("a")
    clock.now += 3600
    for _ in range(60):
        limits.take_op("a")  # holds at most one minute's worth
    with pytest.raises(OpError):
        limits.take_op("a")


def test_byte_bucket_retry_after_is_the_wait_until_it_fits() -> None:
    clock = Clock()
    limits = TokenLimits(Limits(token_body_bytes_per_minute=600), clock)
    limits.take_bytes("a", 500)
    with pytest.raises(OpError) as exc:
        limits.take_bytes("a", 400)
    assert exc.value.details["retry_after"] == 30  # 300 more bytes at 10 bytes/s
    with pytest.raises(OpError) as exc:
        limits.take_bytes("a", 10_000)  # never fits: wait for a full bucket
    assert exc.value.details["retry_after"] == 50
    limits.take_bytes("a", 0)


def test_disk_floor_samples_at_most_once_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    floor = DiskFloor(tmp_path, 10, clock)
    calls = []
    real = __import__("os").statvfs

    def counting(path):  # noqa: ANN001, ANN202
        calls.append(path)
        return real(path)

    monkeypatch.setattr("lattice.server.limits.os.statvfs", counting)
    floor.free_bytes()
    floor.free_bytes()
    clock.now += 0.5
    floor.check()
    assert len(calls) == 1
    clock.now += 1
    assert not floor.low
    assert len(calls) == 2
    high = DiskFloor(tmp_path, 2**62, clock)
    with pytest.raises(OpError) as exc:
        high.check()
    assert exc.value.code == "STORAGE_LOW" and exc.value.http_status == 507
