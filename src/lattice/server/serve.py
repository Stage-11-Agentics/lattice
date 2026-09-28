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


# The soft descriptor limit the server asks for at startup. A launchd service
# on macOS starts at 256, which a server holding streams, task locks, and
# worker files outgrows; macOS refuses a soft limit above its per-process
# maximum, so a refused request retries at OPEN_MAX.
FD_LIMIT_TARGET = 65536
_FD_LIMIT_FALLBACK = 10240


def raise_fd_limit(target: int = FD_LIMIT_TARGET) -> dict[str, int | None]:
    """Raise the soft ``RLIMIT_NOFILE`` toward the hard limit, capped at *target*.

    Never lowers it. Returns ``{"before", "soft", "hard"}`` (``hard`` is
    ``None`` when unlimited), which the startup log line carries.
    """
    import resource

    before, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = target if hard == resource.RLIM_INFINITY else min(hard, target)
    soft = before
    for candidate in (ceiling, min(ceiling, _FD_LIMIT_FALLBACK)):
        if candidate <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (candidate, hard))
        except (ValueError, OSError):
            continue
        soft = candidate
        break
    return {
        "before": before,
        "soft": soft,
        "hard": None if hard == resource.RLIM_INFINITY else hard,
    }


def load_server_config(root: Path) -> ServerConfig:
    try:
        return load_config(root)
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", str(exc)) from exc


def uvicorn_options(config: ServerConfig) -> dict[str, object]:
    """The uvicorn options ``serve`` and the in-process test server share.

    Proxy-header trust comes only from ``trusted_proxies`` (SPEC §8.1): uvicorn
    rewrites the scheme and client address from ``X-Forwarded-Proto`` and
    ``X-Forwarded-For`` only on a connection from a listed peer, taking the
    rightmost ``X-Forwarded-For`` entry that is not itself listed. Both options
    are always passed, because uvicorn otherwise trusts 127.0.0.1, or whatever
    ``FORWARDED_ALLOW_IPS`` names, by default.

    Two uvicorn behaviors operators should know. When every ``X-Forwarded-For``
    entry is listed, uvicorn falls back to the leftmost entry, which the client
    supplied, so list proxies narrowly: a range that also covers clients lets
    them choose their logged address. And matching is by address family: a
    server bound to ``::`` sees an IPv4 proxy as ``::ffff:192.0.2.1``, which
    does not match ``192.0.2.1``, so list the mapped form too.
    """
    proxies = list(config.trusted_proxies)
    return {
        "proxy_headers": bool(proxies),
        "forwarded_allow_ips": proxies,
        "server_header": False,
    }


def serve(root: Path, *, host: str | None = None, port: int | None = None) -> None:
    """Run until SIGTERM or SIGINT. Raises ``OpError`` if the server cannot start."""
    import uvicorn

    from lattice.server.app import create_app

    root = Path(root)
    admin.require_root(root)
    config = load_server_config(root)
    fd = acquire_server_lock(root)
    try:
        fd_limit = raise_fd_limit()
        app = create_app(root, config=config)
        app.state.fd_limit = fd_limit
        uv_config = uvicorn.Config(
            app,
            host=host or config.bind,
            port=config.port if port is None else port,
            workers=1,
            log_config=None,
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=30,
            **uvicorn_options(config),
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
