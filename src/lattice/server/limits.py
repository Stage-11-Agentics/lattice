"""Per-token limits and the disk floor (SPEC §8.1, §8.11).

These run before admission, so a request they refuse never waits for a
project lock and never holds a worker thread. All state is in memory and
touched only on the event loop.

- **In flight:** at most ``max_inflight_per_token`` authenticated requests per
  token at once (open streams excluded); one more gets 429, ``Retry-After: 1``.
- **Rates:** two buckets per token, refilling continuously at
  ``token_ops_per_minute`` operations and ``token_body_bytes_per_minute`` body
  bytes, each holding at most one minute's worth. A request that would
  overdraw either gets 429 with ``Retry-After`` = whole seconds until it fits.
- **Disk floor:** free bytes on the server root's filesystem, read with
  ``statvfs`` at most once a second.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lattice.core.errors import OpError
from lattice.server.config import Limits


def rate_limited(message: str, retry_after: int) -> OpError:
    return OpError("RATE_LIMITED", message, {"retry_after": max(1, retry_after)})


@dataclass
class Bucket:
    """A continuously refilling bucket holding at most ``capacity`` units."""

    capacity: float
    per_second: float
    level: float
    updated: float

    def _refill(self, now: float) -> None:
        self.level = min(self.capacity, self.level + (now - self.updated) * self.per_second)
        self.updated = now

    def take(self, amount: float, now: float) -> int:
        """Take *amount*; returns 0 on success, else the whole seconds until it would fit.

        An amount above the capacity can never fit; it waits for a full bucket.
        """
        self._refill(now)
        if amount <= self.level:
            self.level -= amount
            return 0
        needed = min(amount, self.capacity) - self.level
        return max(1, math.ceil(needed / self.per_second)) if self.per_second > 0 else 60


class TokenLimits:
    """In-flight counters and rate buckets for every token (event-loop only)."""

    def __init__(self, limits: Limits, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self.clock = clock
        self._inflight: dict[str, int] = {}
        self._ops: dict[str, Bucket] = {}
        self._bytes: dict[str, Bucket] = {}

    def _bucket(self, store: dict[str, Bucket], token_id: str, per_minute: int) -> Bucket:
        bucket = store.get(token_id)
        if bucket is None:
            now = self.clock()
            bucket = Bucket(float(per_minute), per_minute / 60.0, float(per_minute), now)
            store[token_id] = bucket
        return bucket

    def enter(self, token_id: str) -> None:
        """Count one more request in flight, or raise 429."""
        count = self._inflight.get(token_id, 0)
        if count >= self.limits.max_inflight_per_token:
            raise rate_limited(
                f"token {token_id} already has {count} requests in flight "
                f"(limit {self.limits.max_inflight_per_token})",
                1,
            )
        self._inflight[token_id] = count + 1

    def leave(self, token_id: str) -> None:
        count = self._inflight.get(token_id, 0) - 1
        if count <= 0:
            self._inflight.pop(token_id, None)
        else:
            self._inflight[token_id] = count

    def take_op(self, token_id: str) -> None:
        bucket = self._bucket(self._ops, token_id, self.limits.token_ops_per_minute)
        wait = bucket.take(1, self.clock())
        if wait:
            raise rate_limited(
                f"token {token_id} is over {self.limits.token_ops_per_minute} operations "
                "per minute",
                wait,
            )

    def take_bytes(self, token_id: str, amount: int) -> None:
        if amount <= 0:
            return
        bucket = self._bucket(self._bytes, token_id, self.limits.token_body_bytes_per_minute)
        wait = bucket.take(amount, self.clock())
        if wait:
            raise rate_limited(
                f"token {token_id} is over {self.limits.token_body_bytes_per_minute} body "
                "bytes per minute",
                wait,
            )


class DiskFloor:
    """Free space under the server root, sampled at most once a second."""

    def __init__(self, root: Path, minimum: int, clock: Callable[[], float] = time.monotonic):
        self.root = Path(root)
        self.minimum = minimum
        self.clock = clock
        self._sampled_at = -math.inf
        self._free = 0

    def free_bytes(self) -> int:
        now = self.clock()
        if now - self._sampled_at >= 1.0:
            st = os.statvfs(self.root)
            self._free = st.f_bavail * st.f_frsize
            self._sampled_at = now
        return self._free

    @property
    def low(self) -> bool:
        return self.free_bytes() < self.minimum

    def check(self) -> None:
        free = self.free_bytes()
        if free < self.minimum:
            raise OpError(
                "STORAGE_LOW",
                f"free disk under the server root ({free} bytes) is below the floor "
                f"({self.minimum} bytes); writes are refused, reads still work",
            )
