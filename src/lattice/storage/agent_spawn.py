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
    sentinel_path,
)

logger = logging.getLogger(__name__)


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

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        for k, v in req.extra_env.items():
            env[k] = v

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                env=env,
                capture_output=True,
                text=True,
                timeout=req.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            duration = time.monotonic() - start
            self._write_sentinel(req, success=False, error="timed out")
            return _failure_result(
                req,
                f"timed out after {req.timeout_seconds}s",
                duration=duration,
            )
        except OSError as exc:
            duration = time.monotonic() - start
            self._write_sentinel(req, success=False, error=str(exc))
            return _failure_result(req, f"failed to spawn: {exc}", duration=duration)

        duration = time.monotonic() - start

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            self._write_sentinel(req, success=False, error=stderr or f"exit {result.returncode}")
            return _failure_result(
                req,
                f"exited with code {result.returncode}. {stderr}",
                duration=duration,
            )

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
        )

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


def _failure_result(req: SpawnRequest, error: str, *, duration: float) -> SpawnResult:
    return SpawnResult(
        agent=req.agent,
        success=False,
        output_text="",
        error=error,
        backend="headless",
        duration_seconds=duration,
    )
