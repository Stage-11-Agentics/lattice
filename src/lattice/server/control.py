"""Control requests: how admin commands act on a project a running server owns (SPEC §8.2).

The admin writes ``<board>/hosted/control/<ULID>.json`` (``{"action", ...}``)
and waits for ``<ULID>.done`` (``{"ok", "result" | "error"}``), which it
reads and deletes. The server checks every project's ``hosted/control/`` at
each admission and every 2 seconds, and runs each request under that
project's locks.

Whether a server is running is decided by the server's own lease: a running
server holds an exclusive flock on ``<server_root>/server.lock`` for its
lifetime.

The admin is not the board's owner, so it creates ``hosted/control/``, writes
its request, and removes the answer with this module's own writers rather
than the board primitives (which refuse ``hosted/`` to anyone but the owner).
Like every server-control write, each fsyncs the file and the directory entry
it changes, and a failed fsync raises (SPEC §8.6). It writes nothing else
under the board.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lattice.core.errors import OpError
from lattice.core.ids import generate_instance_id
from lattice.server.config import SERVER_LOCK

CONTROL_DIR = "control"
DEFAULT_WAIT_SECONDS = 30.0


def _fcntl():  # noqa: ANN202
    try:
        import fcntl
    except ImportError as exc:
        raise OpError(
            "HOSTED_UNSUPPORTED_PLATFORM",
            "Hosted mode needs a POSIX platform (macOS or Linux).",
        ) from exc
    return fcntl


# ---------------------------------------------------------------------------
# The server's own lease
# ---------------------------------------------------------------------------


def try_server_lock(root: Path) -> int | None:
    """Take the exclusive flock on ``<root>/server.lock``; ``None`` if held elsewhere."""
    fcntl = _fcntl()
    fd = os.open(Path(root) / SERVER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def server_running(root: Path) -> bool:
    """Whether a server holds ``<root>/server.lock`` right now."""
    if not (Path(root) / SERVER_LOCK).exists():
        return False
    fd = try_server_lock(root)
    if fd is None:
        return True
    os.close(fd)
    return False


# ---------------------------------------------------------------------------
# Admin side
# ---------------------------------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".tmp.{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def send_request(
    board: Path,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = 0.05,
) -> dict:
    """Write a control request and wait for its answer; returns the ``.done`` object.

    Raises ``OpError`` (``BOARD_BUSY``) when no answer arrives in time; the
    request stays in place, so the server still runs it later.
    """
    control = Path(board) / "hosted" / CONTROL_DIR
    _ensure_dir(control)
    request_id = generate_instance_id().removeprefix("inst_")
    request = {"action": action, **(payload or {})}
    _write_private(control / f"{request_id}.json", (json.dumps(request) + "\n").encode())
    done = control / f"{request_id}.done"
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if done.exists():
            try:
                answer = json.loads(done.read_text(encoding="utf-8"))
            except ValueError:
                time.sleep(poll_seconds)  # written atomically; a parse error is a race
                continue
            _remove(done)
            return answer
        time.sleep(poll_seconds)
    raise OpError(
        "BOARD_BUSY",
        f"the server did not answer control request {request_id} within "
        f"{wait_seconds:g} s; it stays queued and runs when the project is next admitted",
        {"request": request_id},
    )


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

#: action name -> handler(project, request) -> result. Later tickets add
#: ``rotate-epoch`` (H-10a) and ``unload``, ``load``, ``reload``, ``doctor`` (H-22).
ACTIONS: dict[str, Callable[[Any, dict], Any]] = {}


def action(name: str) -> Callable[[Callable[[Any, dict], Any]], Callable[[Any, dict], Any]]:
    def register(fn: Callable[[Any, dict], Any]) -> Callable[[Any, dict], Any]:
        ACTIONS[name] = fn
        return fn

    return register


def pending_requests(board: Path) -> list[Path]:
    """Request files not yet answered, oldest first (ULIDs sort by time)."""
    control = Path(board) / "hosted" / CONTROL_DIR
    try:
        names = os.listdir(control)
    except OSError:
        return []
    return [
        control / name
        for name in sorted(names)
        if name.endswith(".json")
        and not name.startswith(".")
        and not (control / (name[: -len(".json")] + ".done")).exists()
    ]


def answer_unowned(path: Path, answer: dict) -> None:
    """Answer a request for a project this server does not hold, touching nothing
    else: the ``.done`` goes through the same private writer the admin uses."""
    _write_private(path.with_suffix(".done"), (json.dumps(answer, sort_keys=True) + "\n").encode())
    _remove(path)


def _ensure_dir(directory: Path) -> None:
    """``mkdir -p`` that fsyncs the parent of each directory it creates."""
    missing = []
    current = Path(directory)
    while not current.is_dir():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir()
        except FileExistsError:
            continue
        _fsync_dir(path.parent)


def _remove(path: Path) -> None:
    """Unlink a control file and fsync its directory entry's removal."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run_request(project: Any, path: Path, log: Any = None) -> dict:
    """Run one request (caller holds the project's locks); returns the ``.done`` object.

    Any failure becomes an answer, so one bad request never wedges the project.
    """
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request is not an object")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": {"code": "VALIDATION_ERROR", "message": str(exc)}}
    action_name = request.get("action")
    handler = ACTIONS.get(action_name) if isinstance(action_name, str) else None
    if handler is None:
        return {
            "ok": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": f"unsupported control action {request.get('action')!r}",
            },
        }
    try:
        return {"ok": True, "result": handler(project, request)}
    except OpError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except Exception as exc:  # noqa: BLE001 - answered and logged, never raised
        from lattice.server.log import describe_error, exception_fields

        if log is not None:
            log.error(
                "control_request_crashed",
                request=path.stem,
                action=str(request.get("action"))[:64],
                **exception_fields(exc),
            )
        return {
            "ok": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"the server failed running this request ({describe_error(exc)}); "
                "see its log",
            },
        }
