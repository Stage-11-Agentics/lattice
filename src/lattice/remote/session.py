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
4. **Terminal safety.** Plain output (stdout and stderr) on a hosted checkout
   shows every control character except newline and tab as U+FFFD, because
   other people's text reaches this terminal (SPEC §4).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TextIO

from lattice.core.errors import OpError
from lattice.remote import cache_paths
from lattice.remote.binding import Hosted
from lattice.storage.fs import LATTICE_DIR

UNREACHABLE_WINDOW_SECONDS = 15.0
UNREACHABLE_FILE = "unreachable_until"
SERVER_INFO_FILE = "server_info.json"

#: Roots already caught up by this process (a write's post-write sync counts).
_fresh: set[Path] = set()
#: Read locks this process holds, by root.
_locks: dict[Path, contextlib.ExitStack] = {}
#: Roots whose version lines this process already printed.
_announced: set[Path] = set()
#: Whether the offline window was open when this command first looked, by root
#: (a write started inside it does not wait again, SPEC §8.6).
_window_at_start: dict[Path, bool] = {}
#: Whether this process filters unknown-event warnings by the server's types.
_filtering_types = False


def reset_process_state() -> None:
    """Forget per-process state (tests run many commands in one process)."""
    for root in list(_locks):
        release_read_lock(root)
    _fresh.clear()
    _announced.clear()
    _window_at_start.clear()
    _restore_output()
    global _filtering_types
    if _filtering_types:
        from lattice.core.tasks import set_unknown_type_reporter

        set_unknown_type_reporter(None)
        _filtering_types = False


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


def window_open_at_start(hosted: Hosted) -> bool:
    """Whether the offline window was open when this command first asked, before
    its own read phase could open it (SPEC §8.6, "No repeated wait")."""
    key = hosted.root.resolve()
    if key not in _window_at_start:
        _window_at_start[key] = in_unreachable_window(hosted)
    return _window_at_start[key]


def forget_window_at_start(root: Path) -> None:
    """Let the next write on *root* look at the offline window afresh: a process
    that writes again and again (``lattice dashboard``) calls this before each
    write, so each one waits once per outage like a CLI command."""
    _window_at_start.pop(Path(root).resolve(), None)


@contextlib.contextmanager
def _existing_cache_dir(hosted: Hosted, *, create: bool = False) -> Iterator[int | None]:
    """A descriptor of an existing ``cache/`` (never followed, SPEC §9.4), or
    ``None`` when there is none or it is not a real directory: the offline window
    and the server info are best effort. *create* makes a missing ``.lattice/``
    and ``cache/`` first (never through a symlink)."""
    try:
        fd = cache_paths.open_dir(hosted.root, LATTICE_DIR, "cache", create=create)
    except OSError:
        yield None
        return
    try:
        yield fd
    finally:
        os.close(fd)


def open_unreachable_window(hosted: Hosted) -> None:
    """Record the offline window (now plus 15 s), in a binding-only checkout too:
    its missing cache directories are made first (SPEC §8.6, §9.5)."""
    with _existing_cache_dir(hosted, create=True) as fd:
        if fd is None:
            return
        with contextlib.suppress(OSError):
            until = f"{time.time() + UNREACHABLE_WINDOW_SECONDS:.3f}\n"
            cache_paths.write_file(fd, UNREACHABLE_FILE, until.encode("utf-8"))


def sync_ticket(hosted: Hosted) -> object:
    """The cache's sync ticket record now (opaque), for
    :func:`open_unreachable_window_after`."""
    from lattice.remote.cache import sample_ticket

    return sample_ticket(hosted.root)


def open_unreachable_window_after(hosted: Hosted, since: object) -> None:
    """Open the offline window for a request that began at ticket *since*, in
    ticket order (SPEC §9.5): never when a sync that sent its request after
    *since* has succeeded (it saw the server), nor while a sync is in flight
    (its outcome decides)."""
    from lattice.remote.cache import open_window_in_order

    open_window_in_order(hosted.root, since, lambda: open_unreachable_window(hosted))


def close_unreachable_window(hosted: Hosted) -> None:
    """Any successful request to the server ends the offline window."""
    with _existing_cache_dir(hosted) as fd, contextlib.suppress(OSError):
        if fd is not None:
            cache_paths.remove_file(fd, UNREACHABLE_FILE)


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


def _notice(line: str) -> None:
    print(f"lattice: {line}", file=sys.stderr)


def unreachable_notice(
    hosted: Hosted, synced_at: str | None, notify: Callable[[str], None] | None = None
) -> None:
    (notify or _notice)(
        f"cannot reach {hosted.remote}; showing cache as of {synced_at or 'never'}"
    )


def busy_notice(
    hosted: Hosted, synced_at: str | None, notify: Callable[[str], None] | None = None
) -> None:
    (notify or _notice)(f"{hosted.remote} is busy; showing cache as of {synced_at or 'never'}")


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


def ensure_fresh(hosted: Hosted, *, defer_to_running_sync: bool = False) -> None:
    """Catch the cache up once per process (SPEC §9.5); see the module docstring.

    *defer_to_running_sync* (the commands that run until stopped): when a synced
    cache is already being caught up by another sync (a follower's, another
    command's), do not queue behind it for the probe budget. That sync brings
    the cache to the server's head, and the command's own reads wait for its
    apply on the shared read lock, so they see the state before or after it.
    """
    key = hosted.root.resolve()
    if key in _fresh:
        return
    window_open_at_start(hosted)
    _fresh.add(key)
    catch_up_unless_live(hosted, defer_to_running_sync=defer_to_running_sync)


def catch_up_unless_live(
    hosted: Hosted,
    *,
    defer_to_running_sync: bool = False,
    notify: Callable[[str], None] | None = None,
) -> None:
    """One read's freshness step (SPEC §9.5): nothing while a live follower
    keeps the cache fresh; inside the offline window, the cache with its
    notice; otherwise one catch-up. A process that reads again and again
    (``lattice dashboard``) runs it before every read. *notify* receives the
    notice lines (default: stderr)."""
    from lattice.remote.follower import live_follower

    if live_follower(hosted.root):
        return
    state = _state(hosted)
    if state.get("epoch") and in_unreachable_window(hosted):
        unreachable_notice(hosted, state.get("synced_at"), notify)
        return
    if defer_to_running_sync and state.get("epoch") and _sync_in_progress(hosted):
        return
    catch_up_and_report(hosted, notify=notify)


def _sync_in_progress(hosted: Hosted) -> bool:
    """Whether another process or thread holds this cache's sync lock now."""
    import fcntl

    path = hosted.lattice_dir / "locks" / "cache_sync.lock"
    try:
        fd = os.open(path, os.O_RDWR | cache_paths.NOFOLLOW)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def catch_up_and_report(
    hosted: Hosted, *, after_write: bool = False, notify: Callable[[str], None] | None = None
) -> bool:
    """One catch-up with the one-line notices of SPEC §9.5; returns whether the
    cache is now at the server's head. After a write, a failure is only a
    notice (the write succeeded, §3.4 item 4). *notify* receives the notice
    lines (default: stderr)."""
    from lattice.remote.cache import ANY_KIND, SUCCESS_KINDS, catch_up

    release_read_lock(hosted.root)
    try:
        # A read may be served by another process's sync of any outcome; a
        # post-write sync only by a successful one (SPEC §9.5). The offline
        # window opens under the sync lock, in ticket order.
        with cache_access():
            outcome = catch_up(
                hosted.root,
                adopt=SUCCESS_KINDS if after_write else ANY_KIND,
                on_unreachable=None if after_write else lambda: open_unreachable_window(hosted),
            )
    except (OpError, OSError):
        if after_write:
            (notify or _notice)(
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
    if outcome.synced_at is None and not after_write:
        raise _never_synced(hosted, outcome.detail)
    if outcome.kind == "busy":
        busy_notice(hosted, outcome.synced_at, notify)
    else:
        unreachable_notice(hosted, outcome.synced_at, notify)
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
    with _existing_cache_dir(hosted) as fd, contextlib.suppress(OSError):
        if fd is not None:
            text = json.dumps(info, sort_keys=True, indent=2) + "\n"
            cache_paths.write_file(fd, SERVER_INFO_FILE, text.encode("utf-8"))
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
    global _filtering_types
    from lattice.core.tasks import _print_unknown_type, set_unknown_type_reporter

    _filtering_types = True

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
    with cache_access():
        stack.enter_context(read_lock(hosted.root))
    _locks[key] = stack


@contextlib.contextmanager
def cache_access() -> Iterator[None]:
    """Raise an ``OSError`` on a path in a hosted checkout's cache as the
    ``BOARD_IS_CACHE`` of :func:`lattice.remote.cache_paths.cache_access_error`,
    so every surface reports it as it reports any ``OpError``."""
    try:
        yield
    except OSError as exc:
        mapped = cache_paths.cache_access_error(exc)
        if mapped is None:
            raise
        raise mapped from exc


def release_read_lock(root: Path) -> None:
    stack = _locks.pop(Path(root).resolve(), None)
    if stack is not None:
        stack.close()


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


_originals: dict[str, TextIO] = {}


def scrub_output() -> None:
    """Scrub this command's plain stdout **and** stderr (SPEC §4): a
    server-derived error message can carry other people's text too.

    Called by every path that routes a command to a hosted checkout, as soon as
    it knows (``prepare_read``, ``board_or_exit``, ``lattice sync``, ``lattice
    remote ...``, ``lattice cache clear``), and before a routing error about a
    binding is printed (it quotes the committed binding, other people's text).
    Undone at command end by :func:`reset_process_state`."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, _ScrubbingStream):
            continue
        _originals[name] = stream
        setattr(sys, name, _ScrubbingStream(stream))


def _restore_output() -> None:
    for name, original in list(_originals.items()):
        if isinstance(getattr(sys, name), _ScrubbingStream):
            setattr(sys, name, original)
    _originals.clear()


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def prepare_read(hosted: Hosted, *, lock: bool = True) -> Path:
    """Everything a read on *hosted* needs first; returns its cache ``.lattice/``.

    ``lock=False`` is for a command that runs until stopped (``watch``,
    ``wait``, ``dashboard``): it still catches up before its first read (a
    binding-only checkout is bootstrapped; routing errors are typed), but it
    takes no lifetime lock, since it would starve every sync on the machine;
    its own reads take the lock around each read. It does not queue behind a
    sync already in flight (see :func:`ensure_fresh`).
    """
    scrub_output()
    ensure_fresh(hosted, defer_to_running_sync=not lock)
    announce_versions(hosted)
    if lock:
        hold_read_lock(hosted)
    return hosted.lattice_dir
