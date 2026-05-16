"""Clean-room agent spawning primitive.

Public contract: ``spawn_one`` and ``spawn_many`` accept ``SpawnRequest``
records and return ``SpawnResult`` records, regardless of whether the agent
ran via c11 panes, a Terminal window, or a headless ``subprocess.run``.

Backend selection:
- ``select_backend()`` inspects environment + platform and returns a
  ``Backend`` instance from the chain ``c11 -> terminal -> headless``.
- ``LATTICE_SPAWN_BACKEND`` / ``--backend`` force a specific backend; an
  explicit force does NOT fall through (raises ``BackendUnavailableError``).

Layer-boundary note: per ``Decisions.md`` (2026-04-19), this module bends
``core/`` purity by performing environment detection and instantiating
concrete backends from sibling layers. A follow-up task tracks rectifying
the import direction.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_AGENT_TIMEOUT = 600
SENTINEL_SUFFIX = ".done"
DEFAULT_POLL_INTERVAL_S = 0.5

ProgressCallback = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnRequest:
    """A single agent spawn request."""

    agent: str
    prompt_file: Path
    output_file: Path
    label: str
    timeout_seconds: int = DEFAULT_AGENT_TIMEOUT
    extra_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SpawnResult:
    """Result of a single agent spawn."""

    agent: str
    success: bool
    output_text: str
    error: str
    backend: str
    duration_seconds: float


class BackendUnavailableError(RuntimeError):
    """Raised when a forced backend cannot run in the current environment."""


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------


class Backend(ABC):
    """Abstract spawning backend."""

    name: str = "abstract"

    @abstractmethod
    def run(
        self,
        requests: Sequence[SpawnRequest],
        *,
        workspace_label: str,
        on_progress: ProgressCallback | None = None,
    ) -> list[SpawnResult]:
        """Spawn the given requests, wait for all to finish (or time out)."""


# ---------------------------------------------------------------------------
# Agent CLI command builder (pure)
# ---------------------------------------------------------------------------


def _agent_cli_command(agent_type: str, prompt_file: str, output_file: str) -> str | None:
    """Return the shell command string that runs the named agent CLI.

    Mirrors the legacy ``core.review._build_agent_command`` shape so the
    behaviour of the headless backend matches today's ``spawn_agent``.
    """
    instruction = f"Read {prompt_file} and follow the instructions. Write output to {output_file}"
    if agent_type == "claude":
        return f'env -u CLAUDECODE claude --dangerously-skip-permissions -p "{instruction}"'
    if agent_type == "codex":
        return f'codex exec --full-auto --skip-git-repo-check "{instruction}"'
    if agent_type == "gemini":
        return f'gemini -m gemini-3-pro-preview --yolo "{instruction}"'
    return None


def sentinel_path(output_file: Path) -> Path:
    """Compute the ``.done`` sentinel path for a given output file."""
    return output_file.with_suffix(output_file.suffix + SENTINEL_SUFFIX)


# ---------------------------------------------------------------------------
# Backend selector
# ---------------------------------------------------------------------------


_VALID_BACKENDS = {"c11", "terminal", "headless"}


def select_backend(
    *,
    force: str | None = None,
    headless: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Backend:
    """Return a backend instance per the env-aware selection rules.

    Selection chain:
    1. Forced/headless/CI/``LATTICE_SPAWN_BACKEND=headless`` -> ``HeadlessBackend``.
    2. Forced ``c11`` or auto-detected c11 -> ``C11Backend``.
    3. Forced ``terminal`` or auto-detected terminal -> ``TerminalBackend``.
    4. Otherwise -> ``HeadlessBackend``.

    An explicit ``force`` (or ``LATTICE_SPAWN_BACKEND={c11,terminal}``) raises
    ``BackendUnavailableError`` rather than falling through, so operators
    detect misconfiguration instead of silently landing on a different backend.
    """
    env_force = os.environ.get("LATTICE_SPAWN_BACKEND")
    if env_force and env_force not in _VALID_BACKENDS:
        raise BackendUnavailableError(
            f"LATTICE_SPAWN_BACKEND={env_force!r} is not one of {sorted(_VALID_BACKENDS)}"
        )
    effective_force = force or env_force

    ci = os.environ.get("CI", "").lower() in {"1", "true", "yes"}

    # Explicit headless or auto floor.
    if headless or effective_force == "headless" or ci:
        from lattice.storage.agent_spawn import HeadlessBackend

        if on_progress:
            reason = "forced" if (headless or effective_force == "headless") else "CI=1"
            on_progress("backend_selected", f"headless ({reason})")
        return HeadlessBackend()

    if effective_force == "c11":
        if not _c11_available():
            raise BackendUnavailableError(
                "LATTICE_SPAWN_BACKEND=c11 but c11 is not available "
                "(no C11_SOCKET_PATH, missing c11 binary, or c11 identify failed)"
            )
        from lattice.integrations.c11 import C11Backend

        if on_progress:
            on_progress("backend_selected", "c11 (forced)")
        return C11Backend()

    if effective_force == "terminal":
        if not _terminal_available():
            raise BackendUnavailableError(
                "LATTICE_SPAWN_BACKEND=terminal but no supported terminal binary on PATH"
            )
        from lattice.integrations.terminal import TerminalBackend

        if on_progress:
            on_progress("backend_selected", "terminal (forced)")
        return TerminalBackend()

    # Auto-detect chain: c11 -> terminal -> headless.
    if _c11_available():
        from lattice.integrations.c11 import C11Backend

        if on_progress:
            on_progress("backend_selected", "c11 (auto)")
        return C11Backend()

    if sys.stdout.isatty() and _terminal_available():
        from lattice.integrations.terminal import TerminalBackend

        if on_progress:
            on_progress("backend_selected", "terminal (auto)")
        return TerminalBackend()

    from lattice.storage.agent_spawn import HeadlessBackend

    if on_progress:
        on_progress("backend_selected", "headless (auto, no c11/terminal)")
    return HeadlessBackend()


def _c11_available() -> bool:
    """Best-effort check that c11 is reachable in this environment."""
    if not os.environ.get("C11_SOCKET_PATH"):
        return False
    if shutil.which("c11") is None:
        return False
    try:
        result = subprocess.run(
            ["c11", "identify"],
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _terminal_available() -> bool:
    """Best-effort check for a usable terminal launcher on this platform."""
    platform = sys.platform
    if platform == "darwin":
        return shutil.which("osascript") is not None
    if platform.startswith("linux"):
        return shutil.which("gnome-terminal") is not None or shutil.which("xterm") is not None
    return False


# ---------------------------------------------------------------------------
# Polling helper used by detached backends (c11 / terminal)
# ---------------------------------------------------------------------------


def poll_sentinels(
    requests: Sequence[SpawnRequest],
    *,
    backend_name: str,
    started_at: dict[str, float],
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    cancelled: Callable[[], bool] | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[SpawnResult]:
    """Poll per-request ``.done`` sentinels until all land or each times out.

    ``started_at`` maps ``request.agent`` -> ``time.monotonic()`` start. Each
    request's deadline is its own ``timeout_seconds`` from its start. A late
    sentinel that lands after the deadline is ignored: the timeout result is
    final once recorded.
    """
    outstanding = list(requests)
    results: list[SpawnResult] = []

    while outstanding:
        if cancelled is not None and cancelled():
            break
        now = time.monotonic()
        # Collect into a snapshot to avoid mutate-during-iterate.
        for req in list(outstanding):
            sentinel = sentinel_path(req.output_file)
            start = started_at.get(req.agent, now)
            elapsed = now - start
            if sentinel.exists():
                results.append(_finalize_completed(req, backend_name, elapsed))
                outstanding.remove(req)
                if on_progress:
                    on_progress("agent_finished", req.agent)
            elif elapsed >= req.timeout_seconds:
                results.append(
                    SpawnResult(
                        agent=req.agent,
                        success=False,
                        output_text="",
                        error=f"timed out after {req.timeout_seconds}s",
                        backend=backend_name,
                        duration_seconds=elapsed,
                    )
                )
                outstanding.remove(req)
                if on_progress:
                    on_progress("agent_finished", f"{req.agent} (timed out)")
        if outstanding:
            time.sleep(poll_interval)
    return results


def _finalize_completed(req: SpawnRequest, backend_name: str, duration: float) -> SpawnResult:
    """Build a SpawnResult from a request whose sentinel has landed."""
    err_path = req.output_file.with_suffix(req.output_file.suffix + ".err")
    if err_path.exists():
        try:
            err_text = err_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            err_text = f"failed to read .err file: {exc}"
        return SpawnResult(
            agent=req.agent,
            success=False,
            output_text="",
            error=err_text or "agent reported failure",
            backend=backend_name,
            duration_seconds=duration,
        )
    output_text = ""
    if req.output_file.exists():
        try:
            output_text = req.output_file.read_text(encoding="utf-8")
        except OSError as exc:
            return SpawnResult(
                agent=req.agent,
                success=False,
                output_text="",
                error=f"output unreadable: {exc}",
                backend=backend_name,
                duration_seconds=duration,
            )
    if not output_text.strip():
        return SpawnResult(
            agent=req.agent,
            success=False,
            output_text="",
            error="agent produced no output",
            backend=backend_name,
            duration_seconds=duration,
        )
    return SpawnResult(
        agent=req.agent,
        success=True,
        output_text=output_text,
        error="",
        backend=backend_name,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Orchestrator entrypoints
# ---------------------------------------------------------------------------


def spawn_many(
    requests: Sequence[SpawnRequest],
    *,
    workspace_label: str,
    backend: Backend | None = None,
    on_progress: ProgressCallback | None = None,
    force: str | None = None,
    headless: bool = False,
) -> list[SpawnResult]:
    """Spawn multiple requests concurrently. Returns one result per request."""
    if not requests:
        return []
    if backend is None:
        backend = select_backend(force=force, headless=headless, on_progress=on_progress)
    return backend.run(
        requests,
        workspace_label=workspace_label,
        on_progress=on_progress,
    )


def spawn_one(
    request: SpawnRequest,
    *,
    workspace_label: str,
    backend: Backend | None = None,
    on_progress: ProgressCallback | None = None,
    force: str | None = None,
    headless: bool = False,
) -> SpawnResult:
    """Convenience wrapper: spawn a single request."""
    results = spawn_many(
        [request],
        workspace_label=workspace_label,
        backend=backend,
        on_progress=on_progress,
        force=force,
        headless=headless,
    )
    return results[0]


# ---------------------------------------------------------------------------
# Workspace label / scratch dir helpers (used by callers and backends)
# ---------------------------------------------------------------------------


def make_scratch_dir(lattice_dir: Path, workspace_label: str) -> Path:
    """Create a unique scratch dir under ``.lattice/tmp-prompts/``.

    Layout: ``.lattice/tmp-prompts/<workspace_label>-<uuid>/``. Backends and
    callers create per-agent subdirectories beneath this root.
    """
    base = lattice_dir / "tmp-prompts"
    base.mkdir(exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_." else "_" for c in workspace_label)
    sub = base / f"{safe_label}-{uuid.uuid4().hex[:8]}"
    sub.mkdir()
    return sub


# ---------------------------------------------------------------------------
# Concurrent runner used by HeadlessBackend (kept here so backends in
# integrations/ can reuse it without back-importing storage)
# ---------------------------------------------------------------------------


def run_concurrent(
    requests: Sequence[SpawnRequest],
    runner: Callable[[SpawnRequest], SpawnResult],
    *,
    on_progress: ProgressCallback | None = None,
) -> list[SpawnResult]:
    """Run ``runner(request)`` for each request in its own thread."""
    results: list[SpawnResult | None] = [None] * len(requests)

    def _target(idx: int, req: SpawnRequest) -> None:
        if on_progress:
            on_progress("agent_started", req.agent)
        try:
            results[idx] = runner(req)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Spawn runner crashed for agent %s", req.agent)
            results[idx] = SpawnResult(
                agent=req.agent,
                success=False,
                output_text="",
                error=f"runner crashed: {exc}",
                backend="unknown",
                duration_seconds=0.0,
            )
        finally:
            if on_progress and results[idx] is not None:
                on_progress("agent_finished", req.agent)

    threads = [
        threading.Thread(target=_target, args=(i, r), daemon=True) for i, r in enumerate(requests)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r for r in results if r is not None]
