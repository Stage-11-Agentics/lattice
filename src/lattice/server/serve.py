"""``lattice server serve``: run the server in the foreground (SPEC §8.1).

One process, one uvicorn worker. The process holds ``<root>/server.lock`` for
its lifetime, so a second server on the same root fails to start and admin
commands know to use control requests. uvicorn's own loggers are silenced so
stdout carries only the server's JSON lines.
"""

from __future__ import annotations

import os
from pathlib import Path

from lattice.core.errors import OpError
from lattice.server import admin, control
from lattice.server.config import ServerConfig, ServerConfigError, load_config

INSTALL_HINT = (
    "lattice server serve needs the server extra: "
    "uv tool install 'lattice-tracker[server]' (or pip install 'lattice-tracker[server]')"
)


def server_extra_available() -> bool:
    try:
        import sse_starlette  # noqa: F401
        import starlette  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return True


def acquire_server_lock(root: Path) -> int:
    """Take ``<root>/server.lock`` or raise ``OpError`` naming the running server."""
    fd = control.try_server_lock(root)
    if fd is None:
        raise OpError(
            "BOARD_BUSY",
            f"another Lattice server is already running on {root} (it holds server.lock).",
        )
    return fd


def load_server_config(root: Path) -> ServerConfig:
    try:
        return load_config(root)
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", str(exc)) from exc


def serve(root: Path, *, host: str | None = None, port: int | None = None) -> None:
    """Run until SIGTERM or SIGINT. Raises ``OpError`` if the server cannot start."""
    import uvicorn

    from lattice.server.app import create_app

    root = Path(root)
    admin.require_root(root)
    config = load_server_config(root)
    fd = acquire_server_lock(root)
    try:
        app = create_app(root, config=config)
        uv_config = uvicorn.Config(
            app,
            host=host or config.bind,
            port=config.port if port is None else port,
            workers=1,
            log_config=None,
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=30,
            server_header=False,
        )
        _server_class(app.state)(uv_config).run()
    finally:
        os.close(fd)


def _server_class(state: object) -> type:
    """uvicorn's server, except that a graceful SIGTERM exits 0 (SPEC §8.11), and
    open streams end as shutdown begins (uvicorn otherwise waits for them).

    uvicorn re-raises a captured signal after its graceful shutdown, so the
    process would end with 143. SIGINT keeps that behavior.
    """
    import signal

    import uvicorn

    class Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: object) -> None:
            state.registry.close_all_streams()  # type: ignore[attr-defined]
            super().handle_exit(sig, frame)
            captured = getattr(self, "_captured_signals", None)
            if sig == signal.SIGTERM and captured and captured[-1] == sig:
                captured.pop()

    return Server
