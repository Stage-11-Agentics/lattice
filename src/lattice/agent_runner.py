"""Wrapper script that runs an agent CLI and writes the sentinel.

Two modes selected by ``--mode {agent,merge-waiter}`` (default ``agent``).

``agent`` mode (default):
- Reads config from env vars (``LATTICE_AGENT_TYPE``, ``LATTICE_AGENT_PROMPT``,
  ``LATTICE_AGENT_OUTPUT``, ``LATTICE_AGENT_TIMEOUT``).
- Execs the agent CLI shell command produced by
  ``core.agent_spawn._agent_cli_command``.
- Captures the agent's stdout into ``$LATTICE_AGENT_OUTPUT`` if the agent
  did not write the file itself.
- Writes ``$LATTICE_AGENT_OUTPUT.done`` on exit (always — success or failure).
- Writes ``$LATTICE_AGENT_OUTPUT.err`` with the failure cause on non-zero exit.

``merge-waiter`` mode:
- Reads ``$LATTICE_MERGE_UPSTREAM_DIRS`` (a colon-separated list of
  per-agent scratch directories produced by upstream ``agent`` runs).
- Polls each ``<dir>/output.md.done`` until present (or timeout).
- Once all upstream sentinels land, builds the merge prompt from the
  collected outputs and re-execs the wrapper in ``agent`` mode against the
  merge prompt.
- Writes its own sentinel + err sidecar on exit.

This wrapper is the single canonical invocation shape across backends so
the c11 / terminal / headless paths differ only in *how* they launch it.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_TIMEOUT_S = 600
POLL_INTERVAL_S = 0.5


# ---------------------------------------------------------------------------
# CLI / mode dispatch
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lattice.agent_runner")
    parser.add_argument(
        "--mode",
        choices=("agent", "merge-waiter"),
        default="agent",
    )
    args = parser.parse_args(argv)

    if args.mode == "agent":
        return _run_agent_mode()
    return _run_merge_waiter_mode()


# ---------------------------------------------------------------------------
# Agent mode
# ---------------------------------------------------------------------------


def _run_agent_mode() -> int:
    """Run the configured agent CLI and own its sentinel/err files."""
    agent_type = os.environ.get("LATTICE_AGENT_TYPE")
    prompt_file = os.environ.get("LATTICE_AGENT_PROMPT")
    output_file = os.environ.get("LATTICE_AGENT_OUTPUT")
    timeout_s = int(os.environ.get("LATTICE_AGENT_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    label = os.environ.get("LATTICE_AGENT_LABEL", agent_type or "agent")

    if not agent_type or not prompt_file or not output_file:
        sys.stderr.write(
            "agent_runner: missing required env "
            "(LATTICE_AGENT_TYPE / LATTICE_AGENT_PROMPT / LATTICE_AGENT_OUTPUT)\n"
        )
        return 2

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _maybe_set_c11_metadata(label, status="running")

    # Resolve the agent CLI command. Importing inside main keeps this
    # script importable even when lattice is partially built.
    from lattice.core.agent_spawn import _agent_cli_command

    cmd = _agent_cli_command(agent_type, prompt_file, output_file)
    if cmd is None:
        _emit_failure(out_path, f"unknown agent type: {agent_type}", code=2)
        _maybe_set_c11_metadata(label, status="failed")
        return 2

    print(f"[agent_runner] type={agent_type} label={label} timeout={timeout_s}s")
    print(f"[agent_runner] cmd: {cmd}")

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # Stream stdout/stderr live so the c11/terminal pane hosting this
    # wrapper shows output as the agent produces it, rather than dumping
    # a block after the subprocess exits. The old capture_output=True
    # path hid the agent entirely until completion, which nullified the
    # visibility motivation of the c11/terminal backends.
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        _emit_failure(out_path, f"failed to spawn: {exc}", code=2)
        _maybe_set_c11_metadata(label, status="failed")
        return 2

    captured: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                captured.append(line)
        except Exception:  # pragma: no cover - defensive
            pass

    reader_t = threading.Thread(target=_reader, daemon=True)
    reader_t.start()

    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader_t.join(timeout=1)
        _emit_failure(out_path, f"timed out after {timeout_s}s", code=124)
        _maybe_set_c11_metadata(label, status="failed")
        return 124

    # Drain any trailing buffered output before we print the finished marker.
    reader_t.join(timeout=1)

    duration = time.monotonic() - start
    print(f"[agent_runner] finished rc={rc} duration={duration:.1f}s")

    if rc != 0:
        combined = "".join(captured).strip().splitlines()
        first_line = combined[0] if combined else f"exit {rc}"
        _emit_failure(out_path, first_line, code=rc)
        _maybe_set_c11_metadata(label, status="failed")
        return rc

    # Make sure the output file has content. Fall back to captured stdout
    # if the agent CLI didn't write the file itself.
    if not out_path.exists() or not out_path.read_text(encoding="utf-8").strip():
        captured_text = "".join(captured).strip()
        if captured_text:
            out_path.write_text(captured_text + "\n", encoding="utf-8")
        else:
            _emit_failure(out_path, "no output produced", code=2)
            _maybe_set_c11_metadata(label, status="failed")
            return 2

    _write_sentinel(out_path)
    _maybe_set_c11_metadata(label, status="done")
    return 0


# ---------------------------------------------------------------------------
# Merge-waiter mode
# ---------------------------------------------------------------------------


def _run_merge_waiter_mode() -> int:
    """Wait for upstream sentinels, then merge the collected outputs."""
    upstream_raw = os.environ.get("LATTICE_MERGE_UPSTREAM_DIRS", "")
    upstream_dirs = [Path(p) for p in upstream_raw.split(":") if p]
    merge_prompt = os.environ.get("LATTICE_MERGE_PROMPT")
    output_file = os.environ.get("LATTICE_AGENT_OUTPUT")
    timeout_s = int(os.environ.get("LATTICE_AGENT_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    label = os.environ.get("LATTICE_AGENT_LABEL", "merge")

    if not upstream_dirs or not merge_prompt or not output_file:
        sys.stderr.write(
            "agent_runner --mode=merge-waiter: missing required env "
            "(LATTICE_MERGE_UPSTREAM_DIRS / LATTICE_MERGE_PROMPT / LATTICE_AGENT_OUTPUT)\n"
        )
        return 2

    prompt_path = Path(merge_prompt)
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    _maybe_set_c11_metadata(label, status="waiting")

    deadline = time.monotonic() + timeout_s
    pending = list(upstream_dirs)
    landed: list[Path] = []
    print(f"[agent_runner] merge-waiter: waiting on {len(pending)} upstream sentinels")

    while pending and time.monotonic() < deadline:
        for d in list(pending):
            sentinel = d / "output.md.done"
            if sentinel.exists():
                pending.remove(d)
                landed.append(d)
                print(f"[agent_runner] sentinel landed: {sentinel}")
        if pending:
            time.sleep(POLL_INTERVAL_S)

    if not landed:
        _emit_failure(out_path, "no upstream sentinels landed before timeout", code=124)
        _maybe_set_c11_metadata(label, status="failed")
        return 124

    if pending:
        print(
            f"[agent_runner] proceeding with partial inputs ({len(landed)}/{len(upstream_dirs)})"
        )

    # Build the merge inputs section from the upstream outputs.
    sections = []
    for d in upstream_dirs:
        agent = d.name
        out = d / "output.md"
        err = d / "output.md.err"
        if out.exists() and out.read_text(encoding="utf-8").strip():
            sections.append(f"## Review from {agent}\n\n{out.read_text(encoding='utf-8')}")
        elif err.exists():
            sections.append(
                f"## Review from {agent}\n\n*(failed: {err.read_text(encoding='utf-8').strip()})*"
            )
        else:
            sections.append(f"## Review from {agent}\n\n*(no sentinel landed)*")
    inputs_block = "\n\n---\n\n".join(sections)

    # The orchestrator passes a prompt template; we substitute the collected
    # inputs into a known marker and write the final prompt for the merge agent.
    template = prompt_path.read_text(encoding="utf-8")
    final_prompt = template.replace("{merge_inputs}", inputs_block)
    final_prompt_path = prompt_path.parent / "merge_prompt_filled.md"
    final_prompt_path.write_text(final_prompt, encoding="utf-8")

    # Run the merge agent in-process so callers' monkeypatches and the
    # current Python's environment apply uniformly.
    merge_agent = os.environ.get("LATTICE_MERGE_AGENT", "claude")
    print(f"[agent_runner] merge-waiter: invoking merge agent ({merge_agent})")
    os.environ["LATTICE_AGENT_TYPE"] = merge_agent
    os.environ["LATTICE_AGENT_PROMPT"] = str(final_prompt_path)
    os.environ["LATTICE_AGENT_OUTPUT"] = str(out_path)
    os.environ["LATTICE_AGENT_TIMEOUT"] = str(timeout_s)
    os.environ["LATTICE_AGENT_LABEL"] = label
    return _run_agent_mode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_sentinel(output_file: Path) -> None:
    """Touch the ``.done`` sentinel beside ``output_file``."""
    sentinel = output_file.with_suffix(output_file.suffix + ".done")
    sentinel.touch()


def _emit_failure(output_file: Path, message: str, *, code: int) -> None:
    """Record a failure: write sentinel + .err sidecar."""
    err_path = output_file.with_suffix(output_file.suffix + ".err")
    try:
        err_path.write_text(message, encoding="utf-8")
    except OSError:
        pass
    _write_sentinel(output_file)
    sys.stderr.write(f"[agent_runner] FAILED ({code}): {message}\n")


def _maybe_set_c11_metadata(label: str, *, status: str) -> None:
    """Best-effort surface metadata update — silent if c11 is unavailable.

    Called from inside c11 panes so the sidebar reflects per-pane state.
    """
    if not os.environ.get("C11_WORKSPACE_ID"):
        return
    surface = os.environ.get("C11_SURFACE_ID")
    if not surface:
        return
    try:
        subprocess.run(
            ["c11", "set-metadata", "--surface", surface, "--key", "status", "--value", status],
            capture_output=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


# Quote helper kept for downstream callers that need to escape paths into
# the shell command this wrapper runs.
def _shell_quote(value: str) -> str:
    return shlex.quote(value)


if __name__ == "__main__":
    sys.exit(main())
