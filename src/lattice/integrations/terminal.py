"""Terminal backend: spawn agents in macOS Terminal/iTerm or Linux terminal windows.

Each agent runs in its own Terminal window (macOS) or terminal emulator
window (Linux). The orchestrator polls per-agent ``.done`` sentinel files
to learn when each one has finished — same contract as the cmux backend.

This backend is opt-in via ``select_backend`` auto-detection or
``LATTICE_SPAWN_BACKEND=terminal``. Driving Terminal.app via osascript
prompts for Accessibility/Automation permissions on first use; if the user
declines, ``BackendUnavailableError`` causes the selector to fall through
to the headless backend.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from lattice.core.agent_spawn import (
    Backend,
    BackendUnavailableError,
    ProgressCallback,
    SpawnRequest,
    SpawnResult,
    poll_sentinels,
)

logger = logging.getLogger(__name__)


class TerminalBackend(Backend):
    """Spawn agents in detached terminal windows."""

    name = "terminal"

    def run(
        self,
        requests: Sequence[SpawnRequest],
        *,
        workspace_label: str,
        on_progress: ProgressCallback | None = None,
    ) -> list[SpawnResult]:
        if not requests:
            return []

        launcher = self._select_launcher()
        if launcher is None:
            raise BackendUnavailableError(
                "TerminalBackend: no supported terminal launcher found on PATH"
            )

        started_at: dict[str, float] = {}
        repo_root = _find_repo_root()
        for req in requests:
            if on_progress:
                on_progress("agent_started", req.agent)
            launcher(req, workspace_label=workspace_label, repo_root=repo_root)
            started_at[req.agent] = time.monotonic()

        return poll_sentinels(
            requests,
            backend_name=self.name,
            started_at=started_at,
            on_progress=on_progress,
        )

    def _select_launcher(self):
        """Return the launcher callable for the current platform, or None."""
        if sys.platform == "darwin":
            if shutil.which("osascript") is None:
                return None
            return _launch_macos
        if sys.platform.startswith("linux"):
            if shutil.which("gnome-terminal") is not None:
                return _launch_gnome_terminal
            if shutil.which("xterm") is not None:
                return _launch_xterm
            return None
        return None


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------


def _build_runner_invocation(req: SpawnRequest, *, repo_root: Path) -> str:
    """Compose the shell line that launches the agent_runner wrapper."""
    env_pairs = [
        ("LATTICE_AGENT_TYPE", req.agent),
        ("LATTICE_AGENT_PROMPT", str(req.prompt_file)),
        ("LATTICE_AGENT_OUTPUT", str(req.output_file)),
        ("LATTICE_AGENT_TIMEOUT", str(req.timeout_seconds)),
        ("LATTICE_AGENT_LABEL", req.label),
    ]
    env_str = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_pairs)
    python = shlex.quote(sys.executable)
    cd = f"cd {shlex.quote(str(repo_root))}"
    return f"{cd} && env {env_str} {python} -m lattice.agent_runner --mode agent"


def _launch_macos(req: SpawnRequest, *, workspace_label: str, repo_root: Path) -> None:
    """Open a macOS Terminal window and run the agent there."""
    cmd = _build_runner_invocation(req, repo_root=repo_root)
    title = req.label
    # Escape inner double quotes for AppleScript.
    cmd_escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    title_escaped = title.replace('"', '\\"')
    script = (
        f'tell application "Terminal"\n'
        f"    activate\n"
        f'    set newTab to do script "{cmd_escaped}"\n'
        f'    set custom title of newTab to "{title_escaped}"\n'
        f"end tell"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendUnavailableError(f"osascript launch failed: {exc}") from exc
    if result.returncode != 0:
        raise BackendUnavailableError(
            f"osascript launch failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )


def _launch_gnome_terminal(req: SpawnRequest, *, workspace_label: str, repo_root: Path) -> None:
    cmd = _build_runner_invocation(req, repo_root=repo_root)
    try:
        subprocess.Popen(
            ["gnome-terminal", "--title", req.label, "--", "bash", "-lc", cmd],
            close_fds=True,
        )
    except OSError as exc:
        raise BackendUnavailableError(f"gnome-terminal launch failed: {exc}") from exc


def _launch_xterm(req: SpawnRequest, *, workspace_label: str, repo_root: Path) -> None:
    cmd = _build_runner_invocation(req, repo_root=repo_root)
    try:
        subprocess.Popen(
            ["xterm", "-title", req.label, "-e", "bash", "-lc", cmd],
            close_fds=True,
        )
    except OSError as exc:
        raise BackendUnavailableError(f"xterm launch failed: {exc}") from exc


def _find_repo_root() -> Path:
    """Best-effort: walk up looking for a .git or pyproject.toml; fall back to cwd."""
    here = Path(os.getcwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return here
