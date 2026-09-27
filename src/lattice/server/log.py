"""JSON-lines logging (SPEC §8.11).

One JSON object per line to stdout, filtered by ``server.json``'s
``log_level``. Callers pass only IDs, codes, and sizes: never a token secret,
a session cookie, a payload, or plan text (G-7). As a backstop, any string
value shaped like a Lattice token (``lat_tok_…``) is replaced before the line
is written.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}
_TOKEN_RE = re.compile(r"lat_tok_[0-9A-Za-z]+_[A-Za-z0-9_-]+")
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER_RE.sub("Bearer [redacted]", _TOKEN_RE.sub("[redacted]", value))
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class ServerLog:
    """Thread-safe JSON-lines writer."""

    def __init__(self, level: str = "info", stream: TextIO | None = None) -> None:
        self.level = level
        self._threshold = _LEVELS.get(level, 20)
        self._stream = stream
        self._lock = threading.Lock()

    def enabled(self, level: str) -> bool:
        return _LEVELS.get(level, 20) >= self._threshold

    def emit(self, level: str, event: str, **fields: Any) -> None:
        if not self.enabled(level):
            return
        now = datetime.now(timezone.utc)
        record = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
            "level": level,
            "event": event,
        }
        record.update({k: _scrub(v) for k, v in fields.items()})
        line = json.dumps(record, sort_keys=False, default=str, separators=(",", ":"))
        stream = self._stream or sys.stdout
        with self._lock:
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass  # a closed stdout must never break a request

    def debug(self, event: str, **fields: Any) -> None:
        self.emit("debug", event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self.emit("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self.emit("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self.emit("error", event, **fields)


def describe_error(exc: BaseException) -> str:
    """A one-line description with no exception message: the type, plus the errno
    text for an ``OSError`` (a fixed system string, never request data)."""
    name = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"{name} [Errno {exc.errno}] {os.strerror(exc.errno)}"
    return name


def exception_fields(exc: BaseException) -> dict[str, Any]:
    """Log fields for a crash: exception types and stack frames, never messages or
    values, which can hold payload text or credentials (SPEC §8.11, G-7)."""
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append(describe_error(current))
        current = current.__cause__ or current.__context__
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc.__traceback__)
    ]
    return {"exception": type(exc).__name__, "chain": chain, "frames": frames[-40:]}
