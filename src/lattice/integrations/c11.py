"""c11 backend: spawn agents in a dedicated c11 workspace.

For each ``spawn_many`` call this backend:

1. Creates a new workspace via ``c11 new-workspace`` and renames it to
   the workspace label (typically ``review:<short-id>``).
2. Builds a 2x2 pane grid: top-left = first agent (default ``claude``),
   top-right = second (``codex``), bottom-left = third (``gemini``),
   bottom-right = an optional merge slot (kept idle if ``requests`` only
   has three entries — the orchestrator drives the merge fan-in via the
   wrapper, not this backend).
3. Renames each pane's tab to ``<workspace_label> :: <agent>`` per the
   c11 skill's lineage convention, sets a one-line description, and
   seeds surface metadata (``role``, ``task``, ``status``).
4. Sends an ``agent_runner`` invocation to each pane via ``c11 send`` +
   ``c11 send-key enter``.
5. Polls per-agent ``.done`` sentinel files to learn when each finishes —
   identical contract to the terminal/headless backends.

This module only ever invokes the ``c11`` binary; see
``lattice.cli.c11_bridge`` for the canonical note on the OS-layer compat
alias still set by the c11 binary for backward compatibility.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from lattice.cli.c11_bridge import _run_c11 as _bridge_run_c11
from lattice.core.agent_spawn import (
    Backend,
    BackendUnavailableError,
    ProgressCallback,
    SpawnRequest,
    SpawnResult,
    poll_sentinels,
)

logger = logging.getLogger(__name__)


_REF_RE = re.compile(r"\b(workspace|pane|surface):(\d+)\b")


class C11Backend(Backend):
    """Spawn agents in a dedicated c11 workspace + 2x2 pane grid."""

    name = "c11"

    def run(
        self,
        requests: Sequence[SpawnRequest],
        *,
        workspace_label: str,
        on_progress: ProgressCallback | None = None,
    ) -> list[SpawnResult]:
        if not requests:
            return []

        # 1. Create workspace.
        ws_ref = _new_workspace()
        if ws_ref is None:
            raise BackendUnavailableError("c11 new-workspace failed")
        if on_progress:
            on_progress("workspace_created", f"{workspace_label} -> {ws_ref}")

        _rename_workspace(ws_ref, workspace_label)
        _set_workspace_metadata(ws_ref, workspace_label)

        # 2. Build the pane grid. Slot 1 is the auto-created pane; we add
        # up to three more for total of 4 (3 agents + 1 merge slot).
        slots = _build_pane_grid(ws_ref, slot_count=max(len(requests), 1))
        if len(slots) < len(requests):
            # Couldn't even create enough panes — fall back path is the
            # caller's responsibility (BackendUnavailableError lets the
            # selector route to a different backend).
            raise BackendUnavailableError(
                f"c11 backend created {len(slots)} panes but {len(requests)} requested"
            )

        # 3. Wire each request to a slot, decorate the pane, send the runner.
        repo_root = _find_repo_root()
        started_at: dict[str, float] = {}
        for req, slot in zip(requests, slots, strict=False):
            tab_title = f"{workspace_label} :: {req.agent}"
            _rename_tab(ws_ref, slot.surface_ref, tab_title)
            _set_description(
                ws_ref,
                slot.surface_ref,
                f"Lineage: {workspace_label} :: {req.agent}",
            )
            _set_metadata(
                ws_ref,
                slot.surface_ref,
                role="reviewer",
                task=workspace_label,
                status="running",
            )
            _send_runner(ws_ref, slot.surface_ref, req=req, repo_root=repo_root)
            if on_progress:
                on_progress("agent_started", req.agent)
            started_at[req.agent] = time.monotonic()

        # 4. Poll sentinels — same contract as headless/terminal.
        return poll_sentinels(
            requests,
            backend_name=self.name,
            started_at=started_at,
            on_progress=on_progress,
        )


# ---------------------------------------------------------------------------
# Pane grid construction
# ---------------------------------------------------------------------------


class _Slot:
    __slots__ = ("pane_ref", "surface_ref")

    def __init__(self, pane_ref: str | None, surface_ref: str) -> None:
        self.pane_ref = pane_ref
        self.surface_ref = surface_ref


def _build_pane_grid(ws_ref: str, *, slot_count: int) -> list[_Slot]:
    """Build up to four panes in a 2x2 grid; return slots in order TL,TR,BL,BR."""
    slots: list[_Slot] = []
    initial_surface = _initial_surface(ws_ref)
    if initial_surface is None:
        raise BackendUnavailableError(
            "c11 backend: could not resolve initial pane/surface for new workspace"
        )
    slots.append(_Slot(pane_ref=None, surface_ref=initial_surface))
    if slot_count <= 1:
        return slots

    # Slot 2: split right of initial.
    second = _new_pane(ws_ref, direction="right")
    if second is None:
        return slots
    slots.append(second)
    if slot_count <= 2:
        return slots

    # Slot 3: split down of slot 1 (focus first).
    if slots[0].pane_ref is None:
        # We didn't get the initial pane ref; best effort = focus by surface.
        _focus_by_surface(ws_ref, slots[0].surface_ref)
    else:
        _focus_pane(ws_ref, slots[0].pane_ref)
    third = _new_pane(ws_ref, direction="down")
    if third is None:
        return slots
    slots.append(third)
    if slot_count <= 3:
        return slots

    # Slot 4: split down of slot 2 (focus first).
    if slots[1].pane_ref is None:
        _focus_by_surface(ws_ref, slots[1].surface_ref)
    else:
        _focus_pane(ws_ref, slots[1].pane_ref)
    fourth = _new_pane(ws_ref, direction="down")
    if fourth is None:
        return slots
    slots.append(fourth)
    return slots


# ---------------------------------------------------------------------------
# Single-pane primitive (used by triple-mode reviews — LAT-218)
# ---------------------------------------------------------------------------


def spawn_one_in_current_workspace(
    prompt_text: str,
    *,
    tab_title: str,
    description: str,
    cwd: Path,
    on_progress: Callable[[str, str], None] | None = None,
) -> tuple[bool, str]:
    """Split a new pane in the caller's c11 workspace and start an interactive claude.

    Distinct from :class:`C11Backend` (which creates its own workspace + grid).
    This primitive stays *inside* the caller's workspace: the new pane is a
    sibling of whichever surface invoked Lattice, so the user sees the work
    appear right next to them.

    Returns ``(True, surface_ref)`` on success, ``(False, error_message)`` on
    failure. The returned ``surface_ref`` is the ``surface:N`` the caller can
    surface in a "running in pane:N" message.

    Sequence:

    1. Resolve current workspace from ``C11_WORKSPACE_ID``. If unset →
       return failure with a clear message.
    2. ``c11 new-pane --workspace <ws> --direction right --title <tab_title>``
       → parse the new surface ref out of the response.
    3. ``c11 set-description`` to ``description``.
    4. Write the prompt to a scratch file under ``<cwd>/.lattice/tmp-prompts/``.
    5. ``c11 send`` the bootstrap command (``cd <cwd> && claude
       --dangerously-skip-permissions "Read /path/to/prompt.md and follow the
       instructions."``) followed by ``c11 send-key enter`` to submit it.
    6. Return ``(True, surface_ref)``.
    """
    ws_ref = os.environ.get("C11_WORKSPACE_ID")
    if not ws_ref:
        return False, "not inside c11 — run from a c11 surface (C11_WORKSPACE_ID unset)"

    pane = _new_pane(ws_ref, direction="right", title=tab_title)
    if pane is None:
        return False, "c11 new-pane failed — check `c11 new-pane --help` and the c11 daemon"
    surface_ref = pane.surface_ref
    if on_progress:
        on_progress("pane_created", surface_ref)

    _set_description(ws_ref, surface_ref, description)

    prompt_dir = cwd / ".lattice" / "tmp-prompts" / f"trident-{surface_ref.replace(':', '-')}"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / "prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")

    cd_cmd = f"cd {shlex.quote(str(cwd))}"
    instruction = f"Read {shlex.quote(str(prompt_path))} and follow the instructions."
    claude_cmd = f'claude --dangerously-skip-permissions "{instruction}"'
    line = f"{cd_cmd} && {claude_cmd}"

    _bridge_run_c11(
        "send",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        line,
    )
    _bridge_run_c11(
        "send-key",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        "enter",
    )
    if on_progress:
        on_progress("agent_started", surface_ref)

    return True, surface_ref


# ---------------------------------------------------------------------------
# c11 CLI helpers (parse refs out of `OK ...` output)
# ---------------------------------------------------------------------------


def _c11_capture(*args: str) -> str | None:
    """Run c11 with ``args`` and return stdout text on success, else None."""
    import subprocess

    try:
        result = subprocess.run(
            ["c11", *args],
            capture_output=True,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("c11 capture failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "c11 %s failed (exit %d): %s",
            " ".join(args),
            result.returncode,
            result.stderr.strip(),
        )
        return None
    return result.stdout


def _parse_refs(text: str) -> dict[str, str]:
    """Pull ``workspace:N`` / ``pane:N`` / ``surface:N`` refs from c11 output."""
    refs: dict[str, str] = {}
    for kind, num in _REF_RE.findall(text or ""):
        refs.setdefault(kind, f"{kind}:{num}")
    return refs


def _new_workspace() -> str | None:
    out = _c11_capture("new-workspace")
    if not out:
        return None
    return _parse_refs(out).get("workspace")


def _rename_workspace(ws_ref: str, title: str) -> None:
    _bridge_run_c11("rename-workspace", "--workspace", ws_ref, title)


def _set_workspace_metadata(ws_ref: str, label: str) -> None:
    """Best-effort workspace-level metadata."""
    _bridge_run_c11(
        "set-workspace-metadata",
        "--workspace",
        ws_ref,
        "--key",
        "lattice_label",
        "--value",
        label,
    )


def _initial_surface(ws_ref: str) -> str | None:
    """Pull the surface ref of the workspace's initial (only) pane.

    Primary path uses ``c11 list-pane-surfaces --workspace <ref>`` — the
    dedicated command for enumerating surfaces. Verified against the c11
    build shipping on 2026-04-19 (``c11 --help`` lists the command; manual
    validation during LAT-205 impl-cycle-1 confirmed it on workspace:14).

    If the primary command returns nothing (older builds or future CLI
    churn), fall back to ``c11 tree --workspace <ref>``, whose text output
    includes ``surface:<N>`` refs that the shared ``_parse_refs`` helper
    already pulls. The regex grabs the first surface it sees — fresh
    workspaces have exactly one pane with one surface, so this is
    deterministic for our use case.
    """
    out = _c11_capture("list-pane-surfaces", "--workspace", ws_ref)
    if out:
        surface = _parse_refs(out).get("surface")
        if surface:
            return surface
    # Fallback: tree output also contains surface refs.
    tree_out = _c11_capture("tree", "--workspace", ws_ref)
    if not tree_out:
        return None
    return _parse_refs(tree_out).get("surface")


def _new_pane(ws_ref: str, *, direction: str, title: str | None = None) -> _Slot | None:
    args = ["new-pane", "--workspace", ws_ref, "--direction", direction]
    if title is not None:
        args.extend(["--title", title])
    out = _c11_capture(*args)
    if not out:
        return None
    refs = _parse_refs(out)
    surface = refs.get("surface")
    if not surface:
        return None
    return _Slot(pane_ref=refs.get("pane"), surface_ref=surface)


def _focus_pane(ws_ref: str, pane_ref: str) -> None:
    _bridge_run_c11("focus-pane", "--workspace", ws_ref, "--pane", pane_ref)


def _focus_by_surface(ws_ref: str, surface_ref: str) -> None:
    """Best-effort surface focus — used when we never got a pane ref.

    The c11 ``tab-action`` binary doesn't expose a ``focus`` action, so the
    fallback is the surface-focus subset that exists today: ``focus-pane`` is
    pane-scoped, but ``send-key`` against a surface raises focus side-effects
    in practice. If the caller has the pane ref, ``_focus_pane`` is preferred.
    """
    # No safe surface-only focus command in current c11 CLI; the resolved
    # c11 backend always passes pane refs, so this is a no-op fallback.
    return


def _rename_tab(ws_ref: str, surface_ref: str, title: str) -> None:
    _bridge_run_c11(
        "rename-tab",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        title,
    )


def _set_description(ws_ref: str, surface_ref: str, text: str) -> None:
    # c11 only accepts --source values explicit|declare|osc|heuristic;
    # `explicit` is the right choice for an external CLI driver.
    _bridge_run_c11(
        "set-description",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        "--source",
        "explicit",
        text,
    )


def _set_metadata(
    ws_ref: str,
    surface_ref: str,
    *,
    role: str,
    task: str,
    status: str,
) -> None:
    """Seed per-pane metadata (role/task/status). Best-effort."""
    import json as _json

    payload = _json.dumps({"role": role, "task": task, "status": status})
    _bridge_run_c11(
        "set-metadata",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        "--json",
        payload,
    )


def _send_runner(
    ws_ref: str,
    surface_ref: str,
    *,
    req: SpawnRequest,
    repo_root: Path,
) -> None:
    """Send the agent_runner shell command into the pane and press Enter."""
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
    line = f"{cd} && env {env_str} {python} -m lattice.agent_runner --mode agent"

    _bridge_run_c11(
        "send",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        line,
    )
    # Two-call send: c11 send adds the text, send-key enter submits it.
    _bridge_run_c11(
        "send-key",
        "--workspace",
        ws_ref,
        "--surface",
        surface_ref,
        "enter",
    )


def _find_repo_root() -> Path:
    here = Path(os.getcwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return here
