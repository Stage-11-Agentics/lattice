"""Pure gating logic for auto-firing review subprocesses on status transitions.

This module is deliberately filesystem-free (it lives in ``core/``). The
side-effecting bits — locating the ``lattice`` binary, claiming
``review_state``, opening the log file, and spawning the detached subprocess
— live in :mod:`lattice.cli.auto_review`. See LAT-211 for the layer-boundary
rationale: ``core/`` holds pure logic, ``cli/`` wires it via Click and owns
the side effects.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Constants shared between the gating helper and the spawn helper
# ---------------------------------------------------------------------------

#: Subdirectory of ``.lattice/`` where auto-fire log files live.  Created
#: lazily on first spawn.  Never garbage-collected — logs are overwritten
#: per spawn so disk usage is bounded.
DAEMON_DIR_NAME = ".daemon"

#: Actor string written on auto-fired review CLI invocations and on the
#: ``auto_review_spawned`` event.  Distinguishes auto-fired runs from manual
#: ones in the audit trail.
AUTO_REVIEW_ACTOR = "agent:lattice-auto-review"

#: Status slugs that gate auto-fire behavior.  Mapping from status to the
#: review subcommand we'd spawn.
_STATUS_TO_REVIEW_TYPE: dict[str, str] = {
    "review": "code-review",
    "planned": "plan-review",
}

#: Per-gate config key that enables/disables auto-fire.  Both default to
#: ``True``; readers always pass ``True`` as the default to ``dict.get``.
_STATUS_TO_CONFIG_KEY: dict[str, str] = {
    "review": "auto_code_review_on_transition",
    "planned": "auto_plan_review_on_transition",
}

#: Per-gate config key that selects the review mode.  When the resolved
#: mode is ``inline`` the gate is a no-op (nothing to spawn).
_STATUS_TO_MODE_KEY: dict[str, str] = {
    "review": "review_mode",
    "planned": "plan_review_mode",
}

#: Conservative fallback mode per gate when the config key is absent.
#:
#: Note: this DIFFERS from :func:`lattice.core.config.default_config`,
#: which seeds ``review_mode = "single"`` and ``plan_review_mode = "triple"``.
#: Real configs always have these keys, so the fallback is exercised only
#: by sparse configs (mostly tests).  We deliberately fall back to
#: ``inline`` for the planned gate so a key-less config skips auto-fire
#: rather than silently triggering an expensive triple-agent spawn.  The
#: ``review`` gate keeps ``single`` because that's the cheap, common path.
_STATUS_TO_DEFAULT_MODE: dict[str, str] = {
    "review": "single",
    "planned": "inline",
}


# ---------------------------------------------------------------------------
# Public mapping helpers
# ---------------------------------------------------------------------------


def review_type_for_status(new_status: str) -> str | None:
    """Return ``"code-review"`` or ``"plan-review"`` for review-gate statuses.

    Returns ``None`` for any other status — those are not auto-fire gates.
    """
    return _STATUS_TO_REVIEW_TYPE.get(new_status)


def resolve_mode(config: dict, new_status: str) -> str:
    """Resolve the effective review mode for ``new_status`` from config.

    Falls back to the per-gate default when the config key is unset.  Pure
    function — never reads from disk.
    """
    key = _STATUS_TO_MODE_KEY.get(new_status)
    if key is None:
        return _STATUS_TO_DEFAULT_MODE.get(new_status, "single")
    default = _STATUS_TO_DEFAULT_MODE.get(new_status, "single")
    return config.get(key, default)


# ---------------------------------------------------------------------------
# Gating predicate
# ---------------------------------------------------------------------------


def should_auto_fire(
    config: dict,
    new_status: str,
    *,
    no_auto_review_flag: bool,
) -> tuple[bool, str | None]:
    """Decide whether the auto-fire path should run for this transition.

    Returns ``(True, None)`` when all of the following hold:

    1. ``new_status`` is one of the auto-fire gates (``review`` or
       ``planned``).
    2. The corresponding config key is truthy (default ``True``).
    3. The corresponding mode is **not** ``inline`` (inline = "review
       in-session" — there is no subprocess to spawn).
    4. ``--no-auto-review`` was not passed on the ``lattice status``
       invocation.

    Returns ``(False, reason)`` otherwise, where ``reason`` is a stable
    machine-readable code:

    * ``"not_a_review_gate"`` — transition target is not a review/plan gate.
    * ``"disabled_in_config"`` — the corresponding config key is falsy.
    * ``"inline_mode"`` — review_mode / plan_review_mode is ``inline``.
    * ``"no_auto_review_flag"`` — operator passed ``--no-auto-review``.

    Pure function — no filesystem, no review_state read.  The "is a review
    already in flight?" check happens in the side-effect layer (
    :mod:`lattice.cli.auto_review`) after this returns ``True``.
    """
    if new_status not in _STATUS_TO_REVIEW_TYPE:
        return False, "not_a_review_gate"

    if no_auto_review_flag:
        return False, "no_auto_review_flag"

    config_key = _STATUS_TO_CONFIG_KEY[new_status]
    if not config.get(config_key, True):
        return False, "disabled_in_config"

    mode = resolve_mode(config, new_status)
    if mode == "inline":
        return False, "inline_mode"

    return True, None


# ---------------------------------------------------------------------------
# Skip-reason rendering
# ---------------------------------------------------------------------------


def format_skip_reason(reason: str, *, holder_pid: int | None = None) -> str:
    """Return a human-readable one-liner explaining why auto-fire skipped.

    Used by ``status_cmd`` to surface the skip in its end-of-command output
    so an operator can tell at a glance whether the review fired and, if
    not, why.  The returned string is suitable for appending to the
    existing next-step hint.
    """
    if reason == "inline_mode":
        return "auto-review skipped (inline mode)"
    if reason == "disabled_in_config":
        return "auto-review skipped (disabled in config)"
    if reason == "no_auto_review_flag":
        return "auto-review skipped (--no-auto-review)"
    if reason == "not_a_review_gate":
        return "auto-review skipped (not a review gate)"
    if reason == "executable_not_found":
        return "auto-review skipped (lattice executable not found on PATH)"
    if reason == "review_in_flight":
        if holder_pid is not None:
            return f"auto-review skipped (review already in flight, pid {holder_pid})"
        return "auto-review skipped (review already in flight)"
    if reason.startswith("spawn_failed:"):
        detail = reason.split(":", 1)[1]
        return f"auto-review skipped (spawn failed: {detail})"
    return f"auto-review skipped ({reason})"
