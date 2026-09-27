"""The operation framework: every board write is a named operation.

An operation is a class registered under a ``<group>.<verb>`` name. It owns a
frozen ``Params`` dataclass (its command's arguments) and a ``run`` method
holding exactly the rules its command used to hold, raising ``OpError`` where
the command used to exit. Whoever owns the board runs it through
:func:`execute`: the CLI and MCP through ``lattice.boards.LocalBoard``, a
server with the same function. One complete operation::

    from dataclasses import dataclass

    from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
    from lattice.storage.operations import TaskMutationDecision


    @dataclass(frozen=True, kw_only=True)
    class TouchParams(CommonParams):  # adds model, session, triggered_by, on_behalf_of, reason
        task: str  # as the caller gave it: a ULID or a short ID
        note: str | None = None

        def check(self) -> None:  # optional: rules on the inputs alone
            if self.note == "":
                raise OpError("VALIDATION_ERROR", "Note must not be empty.")


    @operation("task.touch")
    class Touch:
        Params = TouchParams

        def run(self, ctx: OpContext, p: TouchParams) -> OpResult:
            task_id = ctx.resolve_task(p.task)
            ctx.require_active(task_id)

            def decide(context):  # runs under the task lock with the replayed state
                data = {"note": p.note} if p.note else {}
                return TaskMutationDecision(events=[ctx.event("x_touched", task_id, data, p)])

            result = ctx.mutate(task_id, decide)
            return OpResult(
                task=result.snapshot, events=result.appended_events, value=result.snapshot
            )

Rules of the pattern:

- ``Params`` fields are the command's arguments and options with the same
  names in snake_case (the option name, not Click's destination), minus
  ``--json``, ``--quiet``, ``--actor`` and ``--name``. ``--file PATH`` becomes
  the file's text; repeatable options are ``tuple[str, ...]``. Supported
  annotations: ``str``, ``int``, ``bool``, ``dict``, ``tuple[str, ...]``, and
  any of them ``| None``.
- ``run`` never prints and never exits. Every rejection is an ``OpError`` with
  the code and message the CLI has always shown.
- Board writes go through ``ctx.mutate`` (or the resource, prose, session and
  config writers), so hooks, the origin stamp, and ``expect_last_event_id``
  apply without the operation handling them.
- ``value`` is the ``data`` object the command prints under ``--json``,
  minus fields the CLI adds from client-side effects.
- Operations live one per module under ``lattice/ops/`` (discovered, so adding
  one edits no shared file) or in a plugin package that names its module in
  the ``lattice.operations`` entry-point group.
"""

from __future__ import annotations

import dataclasses
import json
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lattice.core.errors import HTTP_STATUS, OpError, StateConflict
from lattice.core.events import create_event
from lattice.core.ids import is_short_id, validate_actor, validate_id
from lattice.core.origin import origin_scope
from lattice.storage.operations import (
    AuthoritativeLogError,
    MutationCallback,
    TaskMutationResult,
    TaskPlacementError,
    mutate_task,
    read_task_authority,
)
from lattice.storage.ownership import board_scope
from lattice.storage.ownership import check_board_writable as check_board_markers

__all__ = [
    "HTTP_STATUS",
    "Caller",
    "CommonParams",
    "OpContext",
    "OpError",
    "OpResult",
    "StateConflict",
    "check_path_component",
    "execute",
    "get_operation",
    "operation",
    "parse_params",
    "registered_operations",
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_OP_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_REGISTRY: dict[str, type] = {}


def operation(name: str):  # noqa: ANN201
    """Class decorator: register an operation under ``<group>.<verb>``."""
    if not _OP_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid operation name {name!r}: expected '<group>.<verb>'")

    def register(cls: type) -> type:
        params_cls = getattr(cls, "Params", None)
        if params_cls is None or not dataclasses.is_dataclass(params_cls):
            raise TypeError(f"operation {name}: Params must be a dataclass")
        if not params_cls.__dataclass_params__.frozen:
            raise TypeError(f"operation {name}: Params must be frozen")
        if not callable(getattr(cls, "run", None)):
            raise TypeError(f"operation {name}: missing run(ctx, params)")
        for field_name, annotation in _param_types(params_cls).items():
            if not _supported(annotation):
                raise TypeError(
                    f"operation {name}: parameter {field_name!r} has unsupported type "
                    f"{annotation!r}"
                )
        existing = _REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(
                f"operation {name} is already registered by "
                f"{existing.__module__}.{existing.__qualname__}"
            )
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return register


def get_operation(name: str) -> type:
    """Return the operation class registered as *name* (``UNKNOWN_OP`` if none)."""
    from lattice.ops.discovery import discover

    discover()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise OpError("UNKNOWN_OP", f"Unknown operation '{name}'.", {"op": name})
    return cls


def registered_operations() -> dict[str, type]:
    """Every registered operation by name, after discovery."""
    from lattice.ops.discovery import discover

    discover()
    return dict(sorted(_REGISTRY.items()))


# ---------------------------------------------------------------------------
# Params
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class CommonParams:
    """The provenance options every write command accepts (``common_options``)."""

    model: str | None = None
    session: str | None = None
    triggered_by: str | None = None
    on_behalf_of: str | None = None
    reason: str | None = None

    def provenance(self, *, reason: bool = True) -> dict[str, str | None]:
        """Keyword arguments for ``create_event``; ``reason=False`` omits the reason."""
        kwargs: dict[str, str | None] = {
            "model": self.model,
            "session": self.session,
            "triggered_by": self.triggered_by,
            "on_behalf_of": self.on_behalf_of,
        }
        if reason:
            kwargs["reason"] = self.reason
        return kwargs


def _param_types(params_cls: type) -> dict[str, Any]:
    hints = typing.get_type_hints(params_cls)
    return {f.name: hints[f.name] for f in dataclasses.fields(params_cls) if f.init}


def _union_args(annotation: Any) -> tuple[Any, ...] | None:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return typing.get_args(annotation)
    return None


def _is_str_tuple(annotation: Any) -> bool:
    return typing.get_origin(annotation) is tuple and typing.get_args(annotation) == (str, ...)


def _supported(annotation: Any) -> bool:
    members = _union_args(annotation)
    if members is not None:
        return all(m is type(None) or _supported(m) for m in members)
    return annotation in (str, int, bool, dict) or _is_str_tuple(annotation)


def _type_name(annotation: Any) -> str:
    members = _union_args(annotation)
    if members is not None:
        return " | ".join(_type_name(m) for m in members)
    if annotation is type(None):
        return "null"
    if _is_str_tuple(annotation):
        return "list of str"
    return annotation.__name__


def _coerce(value: Any, annotation: Any) -> tuple[bool, Any]:
    """Return ``(ok, value)``: whether *value* fits *annotation*, and its stored form."""
    members = _union_args(annotation)
    if members is not None:
        for member in members:
            ok, coerced = _coerce(value, member)
            if ok:
                return True, coerced
        return False, value
    if annotation is type(None):
        return value is None, value
    if annotation is bool:
        return isinstance(value, bool), value
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool), value
    if annotation is str:
        return isinstance(value, str), value
    if annotation is dict:
        return isinstance(value, dict), value
    if _is_str_tuple(annotation):
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            return True, tuple(value)
        return False, value
    return False, value


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def parse_params(params_cls: type, obj: Any, *, op_name: str = "operation") -> Any:
    """Build *params_cls* from a JSON object, or raise ``VALIDATION_ERROR``.

    Unknown, missing, and mistyped keys are rejected (``details.reason`` is
    ``UNKNOWN_PARAM``, ``MISSING_PARAM``, or ``WRONG_TYPE``, with
    ``details.param``); a missing key with a declared default takes it. Then
    the class's own ``check()`` runs, if it has one.
    """
    if isinstance(obj, params_cls):
        params = obj
    else:
        if not isinstance(obj, Mapping):
            raise OpError(
                "VALIDATION_ERROR",
                f"{op_name}: params must be an object, got {_value_type(obj)}.",
                {"reason": "WRONG_TYPE", "param": None},
            )
        types_by_name = _param_types(params_cls)
        for key in obj:
            if key not in types_by_name:
                raise OpError(
                    "VALIDATION_ERROR",
                    f"{op_name}: unknown parameter '{key}'.",
                    {"reason": "UNKNOWN_PARAM", "param": key},
                )
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(params_cls):
            if not f.init:
                continue
            if f.name not in obj:
                if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                    raise OpError(
                        "VALIDATION_ERROR",
                        f"{op_name}: missing required parameter '{f.name}'.",
                        {"reason": "MISSING_PARAM", "param": f.name},
                    )
                continue
            annotation = types_by_name[f.name]
            ok, value = _coerce(obj[f.name], annotation)
            if not ok:
                raise OpError(
                    "VALIDATION_ERROR",
                    f"{op_name}: parameter '{f.name}' must be {_type_name(annotation)}, "
                    f"got {_value_type(obj[f.name])}.",
                    {"reason": "WRONG_TYPE", "param": f.name},
                )
            kwargs[f.name] = value
        params = params_cls(**kwargs)
    check = getattr(params, "check", None)
    if callable(check):
        check()
    return params


# ---------------------------------------------------------------------------
# Caller, result, context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who is asking, and from where.

    ``origin`` carries ``op_id`` and ``reported`` (and, on a server,
    ``authenticated``); ``execute`` adds ``op``. ``actor_name`` is a ``--name``
    session name. ``attestations`` are client-observed facts (SPEC §3.4).
    """

    actor: str | None = None
    actor_name: str | None = None
    origin: dict = field(default_factory=dict)
    attestations: dict = field(default_factory=dict)
    expect_last_event_id: str | None = None


@dataclass(frozen=True)
class OpResult:
    """What an operation did. ``idempotent``: it had nothing to do.
    ``replayed``: a server returned a stored result for a retried ``op_id``."""

    task: dict | None = None
    events: list[dict] = field(default_factory=list)
    value: Any = None
    idempotent: bool = False
    replayed: bool = False


@dataclass
class OpContext:
    """What an operation's ``run`` gets besides its params."""

    lattice_dir: Path
    config: dict
    actor: str | dict | None
    caller: Caller
    op_name: str
    run_hooks: bool
    _expectation_pending: bool = True

    @property
    def caller_worktree(self) -> Path | None:
        """The git worktree the operation started in, as its origin reports it."""
        worktree = (self.caller.origin.get("reported") or {}).get("worktree")
        return Path(worktree) if worktree else None

    def resolve_task(self, raw_id: str) -> str:
        """Resolve a ULID or short ID to the task's ULID (``NOT_FOUND`` / ``INVALID_ID``)."""
        from lattice.storage.short_ids import resolve_short_id

        if validate_id(raw_id, "task"):
            return raw_id
        if is_short_id(raw_id):
            normalized = raw_id.upper()
            task_id = resolve_short_id(self.lattice_dir, normalized)
            if task_id is not None:
                return task_id
            raise OpError("NOT_FOUND", f"Short ID '{normalized}' not found.")
        raise OpError("INVALID_ID", f"Invalid task ID format: '{raw_id}'.")

    def require_active(self, task_id: str) -> dict:
        """Return the active task's snapshot.

        ``NOT_FOUND`` when the task is absent or archived; a log that cannot be
        replayed raises ``AuthoritativeLogError``, which ``execute`` reports as
        ``INTEGRITY_ERROR``.
        """
        authority = read_task_authority(self.lattice_dir, task_id, allow_missing=True)
        if authority is None or authority.location != "active":
            raise OpError("NOT_FOUND", f"Task {task_id} not found.")
        return authority.snapshot

    def event(
        self, type: str, task_id: str, data: dict, params: CommonParams, *, reason: bool = True
    ) -> dict:
        """A new event by this operation's actor, with the params' provenance."""
        return create_event(type, task_id, self.actor, data, **params.provenance(reason=reason))

    def mutate(
        self, task_id: str, callback: MutationCallback, **kwargs: Any
    ) -> TaskMutationResult:
        """``mutate_task`` with this operation's config, hook policy, and expectation.

        The caller's ``expect_last_event_id`` applies to the operation's first
        task mutation only.
        """
        expect = self.caller.expect_last_event_id if self._expectation_pending else None
        self._expectation_pending = False
        return mutate_task(
            self.lattice_dir,
            task_id,
            callback,
            self.config,
            run_hooks=self.run_hooks,
            expect_last_event_id=expect,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------

OP_ID_RE = re.compile(r"^op_[0-9A-HJKMNP-TV-Z]{26}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def check_path_component(value: str, label: str) -> None:
    """Refuse a name that is not exactly one safe path component (SPEC §3.1)."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or "/" in value
        or "\\" in value
        or _CONTROL_RE.search(value)
        or value in (".", "..")
    ):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid {label} {value!r}: must be 1 to 128 characters with no path "
            "separator or control character, and not '.' or '..'.",
            {"reason": "UNSAFE_NAME", "param": label},
        )


def check_op_id(op_id: Any) -> None:
    if not isinstance(op_id, str) or not OP_ID_RE.fullmatch(op_id):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid operation id {op_id!r}: expected 'op_' followed by a ULID.",
            {"reason": "INVALID_OP_ID"},
        )


def check_board_writable(board_dir: Path, caller: Caller) -> None:
    """Refuse a write to a board this process does not own (SPEC §6, H-8).

    ``BOARD_IS_CACHE`` on a client cache outside its syncer, ``BOARD_IS_HOSTED``
    on a server-owned board outside the owning server (``owning_board``) and
    offline maintenance.
    """
    check_board_markers(board_dir)


# ---------------------------------------------------------------------------
# Actor resolution (SPEC §3.7; today's require_actor precedence and messages)
# ---------------------------------------------------------------------------


def _invalid_actor(actor: Any) -> OpError:
    return OpError(
        "INVALID_ACTOR",
        f"Invalid actor format: '{actor}'. "
        "Expected prefix:identifier (e.g., human:atin, agent:claude).",
    )


def resolve_actor(lattice_dir: Path, caller: Caller) -> str | dict:
    """Resolve the caller's actor without writing anything."""
    from lattice.core.actors import build_actor_dict
    from lattice.storage.sessions import resolve_session

    if caller.actor_name is not None:
        session = resolve_session(lattice_dir, caller.actor_name)
        if session is None:
            raise OpError(
                "SESSION_NOT_FOUND",
                f"No active session named '{caller.actor_name}'. "
                "Start one with 'lattice session start'.",
            )
        return build_actor_dict(session)
    if caller.actor is not None:
        if not validate_actor(caller.actor):
            raise _invalid_actor(caller.actor)
        return caller.actor
    raise OpError("MISSING_ACTOR", "Either --name (session) or --actor (legacy) is required.")


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def _load_config(board_dir: Path) -> dict:
    return json.loads((board_dir / "config.json").read_text())


def execute(
    board_dir: Path,
    op_name: str,
    params: Any,
    caller: Caller,
    *,
    run_hooks: bool,
    config: dict | None = None,
) -> OpResult:
    """Run one operation against the board at *board_dir* (its ``.lattice/``).

    ``run_hooks``: run the board's hooks in-process after each write, as local
    Lattice always has (``LocalBoard`` passes ``True``; a server ``False``).
    ``config``: the board configuration the caller already loaded. A client
    that runs its own effects afterwards (auto-review, hints) passes the object
    it will use for them, so one pre-write config governs the rules, the hooks,
    and the effects even if a hook edits ``config.json``. Loaded from the board
    when omitted.

    Every storage write the operation makes is confined to this board
    (``BoardPathError``, ``VALIDATION_ERROR``).
    """
    board_dir = Path(board_dir)
    with board_scope(board_dir):
        return _execute(board_dir, op_name, params, caller, run_hooks=run_hooks, config=config)


def _execute(
    board_dir: Path,
    op_name: str,
    params: Any,
    caller: Caller,
    *,
    run_hooks: bool,
    config: dict | None,
) -> OpResult:
    # 1. Only the board's owner writes it.
    check_board_writable(board_dir, caller)

    # 2. Operation, params, and every input that names a file.
    op_cls = get_operation(op_name)
    parsed = parse_params(op_cls.Params, params, op_name=op_name)
    check_op_id(caller.origin.get("op_id"))
    if caller.actor_name is not None:
        check_path_component(caller.actor_name, "session name")

    # 3. The actor, resolved before anything is written.
    if config is None:
        config = _load_config(board_dir)
    actor: str | dict | None = None
    if not getattr(op_cls, "no_actor", False):
        actor = resolve_actor(board_dir, caller)
        on_behalf_of = getattr(parsed, "on_behalf_of", None)
        if on_behalf_of is not None and not validate_actor(on_behalf_of):
            raise _invalid_actor(on_behalf_of)
        if caller.actor_name is not None:
            from lattice.storage.sessions import touch_session

            touch_session(board_dir, caller.actor_name)

    # 4. Every event this operation appends carries its origin.
    origin = {k: v for k, v in caller.origin.items() if k != "op"}
    origin = {"op": op_name, **origin}
    ctx = OpContext(
        lattice_dir=board_dir,
        config=config,
        actor=actor,
        caller=caller,
        op_name=op_name,
        run_hooks=run_hooks,
    )
    # 5. Run it; storage failures surface as typed errors.
    with origin_scope(origin):
        try:
            return op_cls().run(ctx, parsed)
        except TaskPlacementError as exc:
            raise OpError("NOT_FOUND", str(exc)) from exc
        except AuthoritativeLogError as exc:
            raise OpError("INTEGRITY_ERROR", str(exc)) from exc
