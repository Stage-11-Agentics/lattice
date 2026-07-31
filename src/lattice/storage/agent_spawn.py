"""Headless spawning backend.

Uses ``subprocess.run`` with the same agent CLI string as the legacy
``core.review.spawn_agent``. Owns the child PID so ``timeout`` reliably
kills the runner. Writes the ``.done`` sentinel itself so the polling
contract is uniform across backends, even though the headless backend
doesn't strictly need polling.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from lattice.core.agent_spawn import (
    Backend,
    ProgressCallback,
    SpawnRequest,
    SpawnResult,
    _agent_cli_command,
    run_concurrent,
    scrub_host_session_env,
    sentinel_path,
)

logger = logging.getLogger(__name__)

#: How many trailing characters of an agent's stderr/stdout we keep on the
#: ``SpawnResult`` for diagnostics. Enough to capture the tail of a hang or a
#: crash without bloating the failure record.
_TAIL_CHARS = 4000


class HeadlessBackend(Backend):
    """Run agents via ``subprocess.run`` in the current process tree."""

    name = "headless"

    def run(
        self,
        requests: Sequence[SpawnRequest],
        *,
        workspace_label: str,
        on_progress: ProgressCallback | None = None,
    ) -> list[SpawnResult]:
        return run_concurrent(requests, self._run_one, on_progress=on_progress)

    def _run_one(self, req: SpawnRequest) -> SpawnResult:
        cmd = _agent_cli_command(req.agent, str(req.prompt_file), str(req.output_file))
        if cmd is None:
            return _failure_result(req, f"Unknown agent type: {req.agent}", duration=0.0)

        # Strip the firing session's identity (CLAUDECODE + c11/cmux vars) so
        # the headless agent can't rename the host tab or think it's nested.
        env = os.environ.copy()
        scrub_host_session_env(env)
        for k, v in req.extra_env.items():
            env[k] = v

        start = time.monotonic()
        # stdin=DEVNULL: a headless, non-interactive agent must never inherit
        # the parent's stdin. An inherited, never-EOF stdin (a pipe/pty held
        # open by a detached parent chain) is the classic cause of an agent
        # hanging for its entire timeout regardless of workload.
        #
        # start_new_session=True puts the shell in its own process group so a
        # timeout can kill the WHOLE tree. With shell=True, subprocess.run's
        # own kill-on-timeout reaches only the `sh`; the `claude` (and its
        # children) underneath survive as orphans that keep consuming API
        # quota and ~/.claude locks. We manage Popen + killpg ourselves to
        # avoid that leak.
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                cwd=str(req.cwd) if req.cwd is not None else None,
            )
        except OSError as exc:
            duration = time.monotonic() - start
            self._write_sentinel(req, success=False, error=str(exc))
            return _failure_result(req, f"failed to spawn: {exc}", duration=duration, command=cmd)

        try:
            stdout, stderr = proc.communicate(timeout=req.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            stdout, stderr = self._kill_group_and_drain(proc, exc)
            tail = _tail(stderr) or _tail(stdout)
            err = f"timed out after {req.timeout_seconds}s"
            self._write_sentinel(req, success=False, error=tail or err)
            return _failure_result(
                req,
                err,
                duration=duration,
                returncode=None,
                stderr_tail=tail,
                command=cmd,
            )

        duration = time.monotonic() - start
        returncode = proc.returncode

        if returncode != 0:
            stderr_clean = (stderr or "").strip()
            tail = _tail(stderr_clean) or _tail((stdout or "").strip())
            self._write_sentinel(req, success=False, error=tail or f"exit {returncode}")
            return _failure_result(
                req,
                f"exited with code {returncode}. {stderr_clean}",
                duration=duration,
                returncode=returncode,
                stderr_tail=tail,
                command=cmd,
            )

        result = subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        # Ensure output file is populated (may fall back to stdout).
        output_text = ""
        if req.output_file.exists():
            try:
                output_text = req.output_file.read_text(encoding="utf-8")
            except OSError as exc:
                self._write_sentinel(req, success=False, error=str(exc))
                return _failure_result(req, f"output unreadable: {exc}", duration=duration)
        if not output_text.strip() and (result.stdout or "").strip():
            output_text = result.stdout
            try:
                req.output_file.write_text(output_text, encoding="utf-8")
            except OSError as exc:
                self._write_sentinel(req, success=False, error=str(exc))
                return _failure_result(req, f"output write failed: {exc}", duration=duration)
        if not output_text.strip():
            self._write_sentinel(req, success=False, error="no output")
            return _failure_result(req, "produced no output", duration=duration)

        self._write_sentinel(req, success=True)
        return SpawnResult(
            agent=req.agent,
            success=True,
            output_text=output_text,
            error="",
            backend=self.name,
            duration_seconds=duration,
            returncode=returncode,
            command=cmd,
        )

    def _kill_group_and_drain(
        self, proc: subprocess.Popen, exc: subprocess.TimeoutExpired
    ) -> tuple[str, str]:
        """Kill the timed-out process group and return its partial output.

        ``proc`` was launched with ``start_new_session=True`` so it leads its
        own process group; ``os.killpg`` reaps the shell and every descendant
        (the agent and any tools it spawned), preventing orphan leaks. After
        the group dies the pipes close and a final ``communicate()`` drains
        whatever the agent wrote before the wall — the diagnostic that the
        previous ``subprocess.run`` path discarded.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()  # group gone or unreachable — fall back to leader kill
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            stdout, stderr = "", ""
        # Prefer freshly drained output; fall back to whatever the timeout
        # exception already captured.
        stdout = stdout or _as_text(exc.stdout)
        stderr = stderr or _as_text(exc.stderr)
        return stdout, stderr

    def _write_sentinel(self, req: SpawnRequest, *, success: bool, error: str = "") -> None:
        """Write the ``.done`` sentinel (and optional ``.err`` sidecar)."""
        try:
            req.output_file.parent.mkdir(parents=True, exist_ok=True)
            sentinel_path(req.output_file).touch()
            if not success and error:
                err_path: Path = req.output_file.with_suffix(req.output_file.suffix + ".err")
                err_path.write_text(error, encoding="utf-8")
        except OSError:
            logger.exception("Failed to write sentinel for %s", req.agent)


def _failure_result(
    req: SpawnRequest,
    error: str,
    *,
    duration: float,
    returncode: int | None = None,
    stderr_tail: str = "",
    command: str = "",
) -> SpawnResult:
    return SpawnResult(
        agent=req.agent,
        success=False,
        output_text="",
        error=error,
        backend="headless",
        duration_seconds=duration,
        returncode=returncode,
        stderr_tail=stderr_tail,
        command=command,
    )


def _tail(text: str | None) -> str:
    """Return the trailing ``_TAIL_CHARS`` of ``text`` (stripped), or ""."""
    if not text:
        return ""
    text = text.strip()
    return text[-_TAIL_CHARS:]


def _as_text(value: object) -> str:
    """Coerce a TimeoutExpired.stdout/.stderr (str | bytes | None) to str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
