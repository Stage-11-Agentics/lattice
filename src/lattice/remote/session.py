"""The hosted read path of one CLI process (SPEC §9.5, §15, §4).

Before a command reads a hosted checkout's cache it calls :func:`prepare_read`:

1. **Freshness.** Once per process and root, catch the cache up with the
   server, unless a live follower keeps it fresh (``cache/follower.json``:
   ``stream_live_until`` in the future and a live ``pid``). After a catch-up
   that cannot reach the server, ``cache/unreachable_until`` (now + 15 s)
   makes the following reads skip the network. A failed catch-up prints one
   line to stderr and the read continues from the cache; ``--json`` stdout is
   untouched.
2. **Version skew.** One line per command when this client is older than the
   server's minimum or than the server itself, from ``cache/server_info.json``
   (``/v1/info``, refreshed whenever the server's version changes). Unknown-
   event warnings are suppressed for event types the server registers.
3. **Read lock.** The cache's shared read lock is held from here until the
   command ends, so a sync is never seen half-applied. A write in the same
   process releases it first (:func:`release_read_lock`): its post-write sync
   takes the lock exclusively, and ``flock`` does not let one process hold both.
4. **Terminal safety.** Plain output on a hosted checkout shows every control
   character except newline and tab as U+FFFD, because other people's text
   reaches this terminal (SPEC §4).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from lattice.core.errors import OpError
from lattice.remote.binding import Hosted

UNREACHABLE_WINDOW_SECONDS = 15.0
UNREACHABLE_FILE = "unreachable_until"
SERVER_INFO_FILE = "server_info.json"

#: Roots already caught up by this process (a write's post-write sync counts).
_fresh: set[Path] = set()
#: Read locks this process holds, by root.
_locks: dict[Path, contextlib.ExitStack] = {}
#: Roots whose version lines this process already printed.
_announced: set[Path] = set()


def reset_process_state() -> None:
    """Forget per-process state (tests run many commands in one process)."""
    for root in list(_locks):
        release_read_lock(root)
    _fresh.clear()
    _announced.clear()
    _restore_stdout()
    from lattice.core.tasks import set_unknown_type_reporter

    set_unknown_type_reporter(None)


def _cache_dir(hosted: Hosted) -> Path:
    return hosted.lattice_dir / "cache"


def _state(hosted: Hosted) -> dict:
    try:
        data = json.loads((_cache_dir(hosted) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# The offline window
# ---------------------------------------------------------------------------


def _window_path(hosted: Hosted) -> Path:
    return _cache_dir(hosted) / UNREACHABLE_FILE


def in_unreachable_window(hosted: Hosted) -> bool:
    try:
        until = float(_window_path(hosted).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return time.time() < until


def open_unreachable_window(hosted: Hosted) -> None:
    path = _window_path(hosted)
    if not path.parent.is_dir():
        return
    tmp = path.with_name(f".{UNREACHABLE_FILE}.{os.getpid()}.tmp")
    try:
        tmp.write_text(f"{time.time() + UNREACHABLE_WINDOW_SECONDS:.3f}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def close_unreachable_window(hosted: Hosted) -> None:
    """Any successful request to the server ends the offline window."""
    with contextlib.suppress(OSError):
        _window_path(hosted).unlink()


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


def _notice(line: str) -> None:
    print(f"lattice: {line}", file=sys.stderr)


def unreachable_notice(hosted: Hosted, synced_at: str | None) -> None:
    _notice(f"cannot reach {hosted.remote}; showing cache as of {synced_at or 'never'}")


def busy_notice(hosted: Hosted, synced_at: str | None) -> None:
    _notice(f"{hosted.remote} is busy; showing cache as of {synced_at or 'never'}")


def _never_synced(hosted: Hosted, detail: str | None) -> OpError:
    reason = f" ({detail})" if detail else ""
    return OpError(
        "SERVER_UNREACHABLE",
        f"cannot reach {hosted.remote}{reason}, and this checkout has no cache of "
        f"{hosted.label} yet; run the command again when the server is reachable.",
        {"remote": hosted.remote, "project": hosted.project},
    )


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def mark_fresh(hosted: Hosted) -> None:
    _fresh.add(hosted.root.resolve())


def ensure_fresh(hosted: Hosted) -> None:
    """Catch the cache up once per process (SPEC §9.5); see the module docstring."""
    key = hosted.root.resolve()
    if key in _fresh:
        return
    _fresh.add(key)
    from lattice.remote.follower import live_follower

    if live_follower(hosted.root):
        return
    state = _state(hosted)
    if state.get("epoch") and in_unreachable_window(hosted):
        unreachable_notice(hosted, state.get("synced_at"))
        return
    catch_up_and_report(hosted)


def catch_up_and_report(hosted: Hosted, *, after_write: bool = False) -> bool:
    """One catch-up with the one-line notices of SPEC §9.5; returns whether the
    cache is now at the server's head. After a write, a failure is only a
    notice (the write succeeded, §3.4 item 4)."""
    from lattice.remote.cache import catch_up

    release_read_lock(hosted.root)
    try:
        outcome = catch_up(hosted.root)
    except OpError:
        if after_write:
            _notice(
                f"{hosted.remote} took the write, but the cache could not sync; "
                "the next command will retry"
            )
            return False
        raise
    if outcome.kind in ("applied", "unchanged"):
        close_unreachable_window(hosted)
        return True
    if outcome.kind == "incomplete" and not after_write:
        raise OpError(
            "CACHE_INCOMPLETE",
            "the cache was interrupted mid-update and the server is unreachable; "
            f"run `lattice sync` when {hosted.remote} is back.",
            {"root": str(hosted.root)},
        )
    if outcome.kind == "unreachable" and not after_write:
        # Not after a write: the server has just answered, so the next command
        # should try again rather than skip the network.
        open_unreachable_window(hosted)
    if outcome.synced_at is None and not after_write:
        raise _never_synced(hosted, outcome.detail)
    if outcome.kind == "busy":
        busy_notice(hosted, outcome.synced_at)
    else:
        unreachable_notice(hosted, outcome.synced_at)
    return False


# ---------------------------------------------------------------------------
# Version skew (SPEC §15)
# ---------------------------------------------------------------------------


def _client_version() -> str:
    from lattice.remote.http import _client_version as version

    return version()


def read_server_info(hosted: Hosted) -> dict:
    try:
        data = json.loads((_cache_dir(hosted) / SERVER_INFO_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def refresh_server_info(hosted: Hosted, *, force: bool = False) -> dict:
    """``cache/server_info.json``, refreshed from ``/v1/info`` when the server's
    version (``state.json``) differs from the cached one. Offline: the cached copy."""
    info = read_server_info(hosted)
    version = _state(hosted).get("server_version")
    if not force and info and (version is None or info.get("version") == version):
        return info
    from lattice.remote import http
    from lattice.remote.config import resolve_remote

    try:
        data = http.request(resolve_remote(hosted.remote), "GET", "/v1/info").data()
    except (OpError, http.Unreachable, ValueError, KeyError):
        return info
    if not isinstance(data, dict):
        return info
    info = {
        "version": data.get("version"),
        "protocol": data.get("protocol"),
        "min_client_version": data.get("min_client_version"),
        "event_types": sorted(t for t in data.get("event_types") or [] if isinstance(t, str)),
    }
    path = _cache_dir(hosted) / SERVER_INFO_FILE
    if path.parent.is_dir():
        tmp = path.with_name(f".{SERVER_INFO_FILE}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(info, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
    return info


def announce_versions(hosted: Hosted) -> dict:
    """Print the upgrade lines (once per command) and install the unknown-event
    filter; returns the server info used."""
    from lattice.server.protocol import is_older

    info = refresh_server_info(hosted)
    key = hosted.root.resolve()
    if key in _announced or not info:
        return info
    _announced.add(key)
    client = _client_version()
    server = info.get("version")
    minimum = info.get("min_client_version")
    if isinstance(minimum, str) and is_older(client, minimum):
        _notice(
            f"this client ({client}) is older than the server's minimum ({minimum}); "
            "upgrade Lattice"
        )
    elif isinstance(server, str) and is_older(client, server):
        _notice(
            f"server runs Lattice {server}, this client {client}; upgrade to read every event type"
        )
        _suppress_server_types(set(info.get("event_types") or []))
    return info


def _suppress_server_types(known: set[str]) -> None:
    from lattice.core.tasks import _print_unknown_type, set_unknown_type_reporter

    def report(etype: str) -> None:
        if etype not in known:
            _print_unknown_type(etype)

    set_unknown_type_reporter(report)


def check_protocol(hosted: Hosted) -> None:
    """Refuse to write to a server whose ``/v1/info`` names another protocol (AC-48)."""
    from lattice.remote.http import PROTOCOL

    info = read_server_info(hosted) or refresh_server_info(hosted, force=True)
    protocol = info.get("protocol")
    if isinstance(protocol, int) and protocol != PROTOCOL:
        raise OpError(
            "PROTOCOL_MISMATCH",
            f"{hosted.remote} speaks Lattice protocol {protocol}; this client speaks protocol "
            f"{PROTOCOL}. Upgrade Lattice on the side that is older.",
            {"server_protocol": protocol, "client_protocol": PROTOCOL},
        )


# ---------------------------------------------------------------------------
# The read lock
# ---------------------------------------------------------------------------


def hold_read_lock(hosted: Hosted) -> None:
    """Take the cache's shared read lock until :func:`release_read_lock` (or exit)."""
    from lattice.remote.cache import read_lock

    key = hosted.root.resolve()
    if key in _locks:
        return
    stack = contextlib.ExitStack()
    stack.enter_context(read_lock(hosted.root))
    _locks[key] = stack


def release_read_lock(root: Path) -> None:
    stack = _locks.pop(Path(root).resolve(), None)
    if stack is not None:
        stack.close()


@contextlib.contextmanager
def reading(hosted: Hosted) -> Iterator[Path]:
    """Hold the read lock for one bounded read inside a write command."""
    from lattice.remote.cache import read_lock

    key = hosted.root.resolve()
    if key in _locks:
        yield hosted.lattice_dir
        return
    with read_lock(hosted.root) as lattice_dir:
        yield lattice_dir


# ---------------------------------------------------------------------------
# Terminal safety (SPEC §4)
# ---------------------------------------------------------------------------

_CONTROL = {c: "�" for c in (*range(0x20), *range(0x7F, 0xA0)) if c not in (9, 10)}


def scrub_control(text: str) -> str:
    """Every control character except newline and tab, as U+FFFD."""
    return text.translate(_CONTROL)


class _ScrubbingStream:
    """A text stream that replaces control characters before writing.

    JSON output passes through unchanged: ``json.dumps`` escapes every control
    character, so only the newlines between lines remain, and they are kept.
    """

    def __init__(self, inner: TextIO):
        self._inner = inner

    def write(self, text: str) -> int:
        self._inner.write(scrub_control(text))
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_original_stdout: TextIO | None = None


def scrub_stdout() -> None:
    global _original_stdout
    if isinstance(sys.stdout, _ScrubbingStream):
        return
    _original_stdout = sys.stdout
    sys.stdout = _ScrubbingStream(sys.stdout)  # type: ignore[assignment]


def _restore_stdout() -> None:
    global _original_stdout
    if isinstance(sys.stdout, _ScrubbingStream) and _original_stdout is not None:
        sys.stdout = _original_stdout
    _original_stdout = None


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def prepare_read(hosted: Hosted, *, lock: bool = True) -> Path:
    """Everything a read on *hosted* needs first; returns its cache ``.lattice/``."""
    scrub_stdout()
    ensure_fresh(hosted)
    announce_versions(hosted)
    if lock:
        hold_read_lock(hosted)
    return hosted.lattice_dir
