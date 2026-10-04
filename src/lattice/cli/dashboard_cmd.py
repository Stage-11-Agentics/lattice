"""``lattice dashboard`` and ``lattice restart`` commands."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from filelock import FileLock, Timeout

import click

from lattice.cli.helpers import (
    end_read_phase,
    json_envelope,
    json_error_obj,
    load_project_config,
    output_error,
    require_root,
)
from lattice.core.errors import OpError
from lattice.cli.main import cli

_DEFAULT_PORT = 8799

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Module-level state for SIGHUP restart coordination.
_stop_requested = False


def _handle_sighup(signum, frame):  # noqa: ARG001
    """Keep SIGHUP harmless; it does not reload imported Python modules."""


def _handle_sigterm(signum, frame):  # noqa: ARG001
    """Ask the serving loop to stop without waiting inside the signal handler."""
    global _stop_requested
    _stop_requested = True


def _runtime_dir() -> Path:
    """Stable, private location for local dashboard process metadata."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser()
    if not cache_home.is_absolute():
        cache_home = Path.home() / cache_home
    return cache_home / "lattice" / "dashboard"


def _ensure_runtime_dir() -> Path:
    directory = _runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"Dashboard runtime path is not a directory: {directory}")
    os.chmod(directory, 0o700)
    return directory


def _atomic_json(path: Path, value: dict) -> None:
    directory = _ensure_runtime_dir()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _launch_record_path(pid: int) -> Path:
    return _runtime_dir() / f"launch-{pid}.json"


def _restart_request_path(pid: int) -> Path:
    return _runtime_dir() / f"restart-{pid}.request.json"


def _restart_result_path(pid: int) -> Path:
    return _runtime_dir() / f"restart-{pid}.result.json"


def _restart_state_path(port: int) -> Path:
    return _runtime_dir() / f"restart-port-{port}.json"


def _dashboard_launch_argv(host: str, port: int, output_json: bool) -> list[str] | None:
    """Rebuild the original interpreter/entry-point prefix with resolved flags."""
    original = list(getattr(sys, "orig_argv", ()))
    command_indices = [index for index, token in enumerate(original) if token == "dashboard"]
    if not command_indices:
        return None
    command_index = command_indices[0]
    prefix = original[:command_index]
    if len(prefix) < 2 or Path(prefix[-1]).name not in {"lattice", "lattice.exe"}:
        entrypoint = shutil.which("lattice")
        if entrypoint is None:
            return None
        prefix = [sys.executable, entrypoint]
    else:
        prefix[0] = sys.executable
    arguments = ["dashboard", "--host", host, "--port", str(port)]
    if output_json:
        arguments.append("--json")
    return [*prefix, *arguments]


def _make_launch_record(
    lattice_dir: Path,
    host: str,
    port: int,
    readonly: bool,
    output_json: bool,
    server: object,
) -> dict | None:
    argv = _dashboard_launch_argv(host, port, output_json)
    if argv is None:
        return None
    root_override = os.environ.get("LATTICE_ROOT")
    if root_override:
        root_override = str(Path(root_override).expanduser().resolve())
    return {
        "schema": 1,
        "pid": server.pid,
        "boot_id": server.boot_id,
        "board_root": str(Path(lattice_dir).parent.resolve()),
        "argv": argv,
        "cwd": str(Path.cwd().resolve()),
        "host": host,
        "port": port,
        "json": output_json,
        "readonly": readonly,
        "root_override": root_override,
        "started_ns": time.time_ns(),
    }


def _remove_launch_record(pid: int, boot_id: str | None = None) -> None:
    path = _launch_record_path(pid)
    record = _read_json(path)
    if record is not None and (boot_id is None or record.get("boot_id") == boot_id):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _host_for_probe(host: str) -> str:
    if host in {"", "*", "0.0.0.0", "localhost"}:
        return "127.0.0.1"
    return host


def _probe_dashboard(host: str, port: int, timeout: float = 0.4) -> dict | None:
    """Read process identity directly from the dashboard's bound address."""
    import http.client

    probe_host = _host_for_probe(host)
    connection = http.client.HTTPConnection(probe_host, port, timeout=timeout)
    host_header = f"[{probe_host}]:{port}" if ":" in probe_host else f"{probe_host}:{port}"
    try:
        connection.request("GET", "/api/boot", headers={"Host": host_header})
        response = connection.getresponse()
        if response.status != 200:
            return None
        envelope = json.loads(response.read())
        data = envelope.get("data") if envelope.get("ok") is True else None
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    finally:
        connection.close()


def _listening_processes(port: int) -> dict[int, list[str]]:
    """Return listener PIDs and lsof's corresponding local addresses."""
    result = subprocess.run(
        ["lsof", "-nP", "-Fpn", f"-iTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    listeners: dict[int, list[str]] = {}
    current_pid: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            try:
                current_pid = int(line[1:])
            except ValueError:
                current_pid = None
        elif line.startswith("n") and current_pid is not None:
            listeners.setdefault(current_pid, []).append(line[1:])
    return listeners


def _host_from_listener_names(names: list[str], port: int) -> str:
    for name in names:
        value = name.removesuffix(" (LISTEN)")
        if value.startswith("[") and "]:" in value:
            host, _separator, _bound_port = value[1:].rpartition("]:")
        else:
            host, separator, bound_port = value.rpartition(":")
            if not separator or bound_port != str(port):
                continue
        if host == "*":
            return "0.0.0.0"
        if host:
            return host
    return "127.0.0.1"


def _port_is_bindable(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        return True
    except OSError:
        return False


def _process_cwd(pid: int) -> Path | None:
    result = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("n") and line[1:]:
            return Path(line[1:]).expanduser()
    return None


def _legacy_board_root(pid: int) -> tuple[Path | None, str | None]:
    """Recover a legacy process's root override without printing its environment."""
    root_override = None
    try:
        result = subprocess.run(
            ["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None:
        for token in result.stdout.split():
            if token.startswith("LATTICE_ROOT="):
                root_override = Path(token.partition("=")[2]).expanduser()
                break
    if root_override is not None:
        candidate = root_override.resolve()
        if (candidate / ".lattice").is_dir():
            return candidate, str(candidate)

    cwd = _process_cwd(pid)
    if cwd is not None:
        try:
            for candidate in (cwd.resolve(), *cwd.resolve().parents):
                if (candidate / ".lattice").is_dir():
                    return candidate, None
        except OSError:
            pass
    current_override = os.environ.get("LATTICE_ROOT")
    if current_override:
        candidate = Path(current_override).expanduser().resolve()
        if (candidate / ".lattice").is_dir():
            return candidate, str(candidate)
    return None, None


def _legacy_restart_message(pid: int, port: int, names: list[str]) -> str:
    host = _host_from_listener_names(names, port)
    root, root_override = _legacy_board_root(pid)
    if root is None:
        current_override = os.environ.get("LATTICE_ROOT")
        candidates = [Path(current_override).expanduser()] if current_override else []
        candidates.extend((Path.cwd(), *Path.cwd().parents))
        for candidate in candidates:
            candidate = candidate.resolve()
            if (candidate / ".lattice").is_dir():
                root = candidate
                root_override = str(candidate) if current_override else None
                break

    dashboard = f"lattice dashboard --host {shlex.quote(host)} --port {port}"
    if root is not None:
        if root_override:
            launch = f"LATTICE_ROOT={shlex.quote(root_override)} {dashboard}"
            context = f"with board root {root}"
        else:
            launch = f"cd {shlex.quote(str(root))} && {dashboard}"
            context = f"from board root {root}"
    else:
        launch = f"cd <original-board-root> && {dashboard}"
        context = "from the original board root (the directory containing .lattice)"
    return (
        f"Dashboard PID {pid} on port {port} predates restart metadata and was left untouched. "
        f"Stop it with `kill {pid}`, then run `{launch}` {context}."
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
    except OSError:
        result = None
    if result is not None:
        state = result.stdout.strip()
        if result.returncode == 0 and state:
            # A zombie has exited and cannot retain a listener, even if its parent
            # has not reaped it yet.
            return not state.startswith("Z")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail_log(path: Path, max_chars: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
    except OSError as exc:
        return f"Could not read startup log: {exc}"


def _wait_for_free_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            listeners = _listening_processes(port)
        except OSError:
            return False
        if not listeners and _port_is_bindable(host, port):
            return True
        time.sleep(0.1)
    return False


def _terminate_started_process(process: subprocess.Popen, port: int) -> None:  # noqa: ANN001
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    # The caller reports any unexpected listener that remains.
    try:
        _listening_processes(port)
    except OSError:
        pass


def _reuse_completed_restart(port: int, requested_ns: int) -> bool:
    state = _read_json(_restart_state_path(port))
    if (
        state is None
        or state.get("status") != "complete"
        or not isinstance(state.get("completed_ns"), int)
        or state["completed_ns"] < requested_ns
    ):
        return False
    pid = state.get("new_pid")
    boot_id = state.get("new_boot_id")
    host = state.get("host")
    if not isinstance(pid, int) or not isinstance(boot_id, str) or not isinstance(host, str):
        return False
    try:
        listeners = _listening_processes(port)
    except OSError:
        return False
    if set(listeners) != {pid}:
        return False
    boot = _probe_dashboard(host, port)
    if boot is None or boot.get("pid") != pid or boot.get("boot_id") != boot_id:
        return False
    click.echo(
        f"Dashboard restart already completed on port {port} "
        f"(PID {pid}, boot {boot_id}); reusing that healthy process."
    )
    return True


def _find_free_port(host: str, near: int) -> int | None:
    """Return an available port close to *near*, or ``None`` on failure."""
    for candidate in range(near + 1, near + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, candidate))
                return candidate
        except OSError:
            continue
    return None


@cli.command("dashboard")
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port to bind to. Defaults to dashboard_port in config, or 8799.",
)
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def dashboard_cmd(host: str, port: int | None, output_json: bool) -> None:
    """Launch a read-only local web dashboard.

    ``lattice restart`` stops this process and starts a new one with the same
    board, bind address, port, and output mode. SIGHUP does not reload Python.

    On a checkout bound to a server, it reads the checkout's cache, kept caught
    up by an embedded follower, and writes to the server as the browser actor.
    """
    lattice_dir = require_root(output_json)

    # Resolve port: CLI flag > config.dashboard_port > 8799
    if port is None:
        config = load_project_config(lattice_dir)
        port = config.get("dashboard_port", _DEFAULT_PORT)

    # Non-loopback binds are forced into read-only mode
    readonly = host not in _LOOPBACK_HOSTS
    if readonly:
        click.echo(
            "Warning: dashboard is exposed on the network — writes are disabled. "
            "Bind to 127.0.0.1 for local-only access with full write support.",
            err=True,
        )

    global _stop_requested
    _stop_requested = False
    previous_hup = None
    previous_term = None
    if hasattr(signal, "SIGHUP"):
        previous_hup = signal.signal(signal.SIGHUP, _handle_sighup)
    if hasattr(signal, "SIGTERM"):
        previous_term = signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        with contextlib.ExitStack() as stack:
            target = _dashboard_target(lattice_dir, stack, output_json)
            _serve(lattice_dir, host, port, readonly, output_json, target)
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
        if previous_hup is not None:
            signal.signal(signal.SIGHUP, previous_hup)


def _dashboard_target(lattice_dir, stack, is_json):  # noqa: ANN001, ANN202
    """The board to serve: ``None`` for a local board (the server builds its own),
    or, on a bound checkout, the server's board with an embedded follower that
    runs until *stack* closes (SPEC §9.6). A graceful dashboard stop closes
    the stack so the follower clears its freshness record on the way out."""
    from lattice.cli.helpers import hosted_or_exit

    root = lattice_dir.parent
    hosted = hosted_or_exit(root, is_json)
    if hosted is None:
        return None
    from lattice.boards import resolve_board
    from lattice.dashboard.bound import bound_dashboard

    try:
        target = stack.enter_context(bound_dashboard(resolve_board(root)))
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)

    return target


def _serve(lattice_dir, host, port, readonly, output_json, target):  # noqa: ANN001, ANN202, PLR0913
    """Serve one process image; SIGTERM stops it after a bounded write drain."""
    global _stop_requested

    from lattice.dashboard import server as dashboard_server

    try:
        server = dashboard_server.create_server(
            lattice_dir, host, port, readonly=readonly, board=target
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            alt = _find_free_port(host, port)
            hint = (
                f"  lattice dashboard --port {alt}" if alt else "  lattice dashboard --port <PORT>"
            )
            msg = (
                f"Port {port} is already in use — is another dashboard running?\n"
                f"You can stop the other process, or start on a free port:\n\n"
                f"{hint}"
            )
            code = "PORT_IN_USE"
        else:
            msg = str(exc)
            code = "BIND_ERROR"
        if output_json:
            click.echo(json_envelope(False, error=json_error_obj(code, msg)))
        else:
            click.echo(f"Error: {msg}", err=True)
        raise SystemExit(1)

    boot_id = server.boot_id
    launch_record = _make_launch_record(
        Path(lattice_dir), host, port, readonly, output_json, server
    )
    if launch_record is None:
        click.echo(
            "Warning: this launch context could not be recorded; a later restart will "
            "require manual stop/start.",
            err=True,
        )
    else:
        try:
            _atomic_json(_launch_record_path(os.getpid()), launch_record)
        except OSError as exc:
            click.echo(f"Warning: could not write dashboard launch metadata: {exc}", err=True)
            launch_record = None

    url = f"http://{host}:{port}/"
    if output_json:
        click.echo(json_envelope(True, data={"host": host, "port": port, "url": url}))
    else:
        click.echo(f"Lattice dashboard: {url}")
        click.echo("Press Ctrl+C to stop.")
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        # Use handle_request with a short timeout so a signal received just
        # before entering the loop cannot be lost to serve_forever's reset of
        # BaseServer's private shutdown flag.
        server.timeout = 0.1
        while True:
            while not _stop_requested:
                try:
                    server.handle_request()
                except KeyboardInterrupt:
                    _stop_requested = True

            request = _read_json(_restart_request_path(os.getpid()))
            request_id = (
                request.get("request_id")
                if request is not None and request.get("pid") == os.getpid()
                else None
            )
            drained, pending = server.begin_write_drain(
                timeout=dashboard_server.WRITE_DRAIN_TIMEOUT
            )
            if not drained:
                reason = (
                    f"{pending} admitted write(s) did not finish before the "
                    f"{dashboard_server.WRITE_DRAIN_TIMEOUT:g}-second drain deadline"
                )
                if isinstance(request_id, str):
                    try:
                        _atomic_json(
                            _restart_result_path(os.getpid()),
                            {
                                "request_id": request_id,
                                "pid": os.getpid(),
                                "boot_id": boot_id,
                                "status": "drain_aborted",
                                "reason": reason,
                                "completed_ns": time.time_ns(),
                            },
                        )
                    except OSError as exc:
                        click.echo(f"Warning: could not record restart result: {exc}", err=True)
                click.echo(
                    f"Dashboard restart canceled: {reason}; the old dashboard remains available.",
                    err=True,
                )
                _stop_requested = False
                continue

            if isinstance(request_id, str):
                try:
                    _atomic_json(
                        _restart_result_path(os.getpid()),
                        {
                            "request_id": request_id,
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            "status": "stopped",
                            "completed_ns": time.time_ns(),
                        },
                    )
                except OSError as exc:
                    click.echo(f"Warning: could not record restart result: {exc}", err=True)
            break
    finally:
        server.server_close()
        _remove_launch_record(os.getpid(), boot_id)


@cli.command("restart")
@click.option(
    "--port",
    default=None,
    type=int,
    help="Port of the dashboard to restart. Defaults to dashboard_port in config, or 8799.",
)
def restart_cmd(port: int | None) -> None:
    """Stop and start the listening dashboard with its recorded launch context."""
    if port is None:
        lattice_dir = require_root(False)
        config = load_project_config(lattice_dir)
        end_read_phase(lattice_dir)  # before lsof (SPEC §9.4)
        port = config.get("dashboard_port", _DEFAULT_PORT)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        click.echo(f"Error: invalid dashboard port {port!r}.", err=True)
        raise SystemExit(1)
    if not hasattr(signal, "SIGTERM"):
        click.echo(
            "Error: graceful dashboard restart is not supported on this platform.", err=True
        )
        raise SystemExit(1)

    requested_ns = time.time_ns()
    try:
        directory = _ensure_runtime_dir()
    except OSError as exc:
        click.echo(
            f"Error: cannot access the private dashboard runtime directory: {exc}", err=True
        )
        raise SystemExit(1) from exc

    lock_path = directory / f"restart-port-{port}.lock"
    lock = FileLock(lock_path, timeout=120)
    try:
        lock.acquire()
    except Timeout:
        click.echo(
            f"Error: another restart on port {port} did not finish within 120 seconds.", err=True
        )
        raise SystemExit(1) from None

    try:
        os.chmod(lock_path, 0o600)
        try:
            if _reuse_completed_restart(port, requested_ns):
                return
            listeners = _listening_processes(port)
        except OSError as exc:
            click.echo(f"Error: could not inspect listeners on port {port}: {exc}", err=True)
            raise SystemExit(1) from exc

        if not listeners:
            click.echo(f"Error: no process found listening on port {port}.", err=True)
            raise SystemExit(1)
        if len(listeners) != 1:
            pids = ", ".join(str(pid) for pid in sorted(listeners))
            click.echo(
                f"Error: port {port} has multiple listener processes ({pids}); no process was signalled.",
                err=True,
            )
            raise SystemExit(1)

        pid, listener_names = next(iter(listeners.items()))
        launch_path = _launch_record_path(pid)
        record = _read_json(launch_path)
        if record is None:
            click.echo(_legacy_restart_message(pid, port, listener_names), err=True)
            raise SystemExit(1)
        if (
            record.get("schema") != 1
            or record.get("pid") != pid
            or record.get("port") != port
            or not isinstance(record.get("host"), str)
            or not isinstance(record.get("boot_id"), str)
            or not isinstance(record.get("argv"), list)
            or not all(isinstance(value, str) for value in record.get("argv", []))
        ):
            click.echo(
                f"Error: launch metadata for dashboard PID {pid} on port {port} is invalid; "
                "the process was left untouched.",
                err=True,
            )
            raise SystemExit(1)

        host = record["host"]
        old_boot_id = record["boot_id"]
        boot = _probe_dashboard(host, port)
        if boot is None or boot.get("pid") != pid or boot.get("boot_id") != old_boot_id:
            click.echo(
                f"Error: dashboard PID {pid} on port {port} did not match its recorded boot identity; "
                "the process was left untouched.",
                err=True,
            )
            raise SystemExit(1)

        cwd = Path(record.get("cwd", ""))
        board_root = Path(record.get("board_root", ""))
        if not cwd.is_dir() or not board_root.is_dir() or not (board_root / ".lattice").is_dir():
            click.echo(
                f"Error: the recorded dashboard launch directory or board root no longer exists; "
                f"dashboard PID {pid} was left untouched. Check {launch_path}.",
                err=True,
            )
            raise SystemExit(1)

        request_id = uuid.uuid4().hex
        request_path = _restart_request_path(pid)
        result_path = _restart_result_path(pid)
        for stale in (request_path, result_path):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        _atomic_json(
            request_path,
            {
                "request_id": request_id,
                "pid": pid,
                "boot_id": old_boot_id,
                "requested_ns": requested_ns,
            },
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass
            click.echo(f"Error: could not stop dashboard PID {pid}: {exc}.", err=True)
            raise SystemExit(1) from exc

        stop_deadline = time.monotonic() + 15
        while _pid_is_alive(pid) and time.monotonic() < stop_deadline:
            result = _read_json(result_path)
            if (
                result is not None
                and result.get("request_id") == request_id
                and result.get("status") == "drain_aborted"
            ):
                current = _probe_dashboard(host, port)
                try:
                    request_path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    result_path.unlink()
                except FileNotFoundError:
                    pass
                if current is not None and current.get("boot_id") == old_boot_id:
                    click.echo(
                        f"Error: dashboard restart canceled: {result.get('reason')}; "
                        f"PID {pid} remains available on port {port} (boot {old_boot_id}).",
                        err=True,
                    )
                else:
                    click.echo(
                        f"Error: dashboard restart drain was canceled, but the old process "
                        f"could not be verified on port {port}; no replacement was started.",
                        err=True,
                    )
                raise SystemExit(1)
            time.sleep(0.1)

        if _pid_is_alive(pid):
            current = _probe_dashboard(host, port)
            if current is not None and current.get("boot_id") == old_boot_id:
                message = (
                    f"Error: dashboard PID {pid} is still serving on port {port}; "
                    "the stop did not finish, so no replacement was started."
                )
            else:
                message = (
                    f"Error: dashboard PID {pid} did not stop within 15 seconds; "
                    "no replacement was started."
                )
            click.echo(message, err=True)
            raise SystemExit(1)

        if not _wait_for_free_port(host, port, timeout=3):
            click.echo(
                f"Error: dashboard PID {pid} exited, but port {port} is not free; "
                "no replacement was started.",
                err=True,
            )
            raise SystemExit(1)
        _remove_launch_record(pid, old_boot_id)

        root_override = record.get("root_override")
        environment = os.environ.copy()
        if isinstance(root_override, str) and root_override:
            environment["LATTICE_ROOT"] = root_override
        else:
            environment.pop("LATTICE_ROOT", None)

        log_path = directory / f"restart-{port}-{request_id[:10]}.log"
        try:
            log_path.touch(mode=0o600, exist_ok=False)
            os.chmod(log_path, 0o600)
            with log_path.open("ab") as log_stream:
                process = subprocess.Popen(
                    record["argv"],
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except (OSError, ValueError) as exc:
            click.echo(
                f"Error: dashboard PID {pid} stopped, but the new process could not start: {exc}. "
                f"Log: {log_path}.",
                err=True,
            )
            raise SystemExit(1) from exc

        new_boot: dict | None = None
        start_reason = "new process did not become healthy before the startup deadline"
        start_deadline = time.monotonic() + 15
        while time.monotonic() < start_deadline:
            if process.poll() is not None:
                start_reason = f"new process exited with status {process.returncode}"
                break
            new_boot = _probe_dashboard(host, port)
            if new_boot is not None:
                try:
                    new_listeners = _listening_processes(port)
                except OSError as exc:
                    start_reason = f"could not verify the new listener: {exc}"
                    break
                if (
                    new_boot.get("pid") == process.pid
                    and new_boot.get("boot_id") != old_boot_id
                    and set(new_listeners) == {process.pid}
                ):
                    new_record = _read_json(_launch_record_path(process.pid))
                    if (
                        new_record is not None
                        and new_record.get("boot_id") == new_boot.get("boot_id")
                        and new_record.get("port") == port
                    ):
                        completed_ns = time.time_ns()
                        try:
                            _atomic_json(
                                _restart_state_path(port),
                                {
                                    "status": "complete",
                                    "completed_ns": completed_ns,
                                    "old_pid": pid,
                                    "old_boot_id": old_boot_id,
                                    "new_pid": process.pid,
                                    "new_boot_id": new_boot["boot_id"],
                                    "host": host,
                                },
                            )
                        except OSError as exc:
                            click.echo(
                                f"Warning: restart succeeded, but concurrent restart state "
                                f"could not be recorded: {exc}",
                                err=True,
                            )
                        for stale in (request_path, result_path):
                            try:
                                stale.unlink()
                            except FileNotFoundError:
                                pass
                        click.echo(
                            f"Dashboard restarted with current Python code on port {port} "
                            f"(PID {pid} → {process.pid}; boot {new_boot['boot_id']})."
                        )
                        return
                    start_reason = "new listener did not write valid launch metadata"
                    break
            time.sleep(0.1)

        _terminate_started_process(process, port)
        try:
            remaining = _listening_processes(port)
        except OSError as exc:
            remaining = {}
            start_reason += f"; could not inspect the port after cleanup: {exc}"
        listener_note = (
            f" Unexpected listener(s) remain: {', '.join(map(str, sorted(remaining)))}."
            if remaining
            else " The port is free."
        )
        click.echo(
            f"Error: dashboard PID {pid} stopped, but the new process failed: {start_reason}. "
            f"Log: {log_path}.{listener_note}\n{_tail_log(log_path)}",
            err=True,
        )
        raise SystemExit(1)
    finally:
        lock.release()
