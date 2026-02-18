"""Session storage — create, read, update, end, and archive sessions.

Sessions live in ``.lattice/sessions/`` with an ``index.json`` for serial
counters and active session tracking.  Each active session is a JSON file
named ``<disambiguated_name>.json``.  Ended sessions move to
``sessions/archive/``.

All writes to the session index are lock-protected via the ``sessions_index``
lock key.
"""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.actors import ActorIdentity, validate_base_name, validate_session_creation
from lattice.core.events import utc_now
from lattice.core.ids import generate_session_id
from lattice.storage.fs import atomic_write
from lattice.storage.locks import lattice_lock

# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

_SESSIONS_DIR = "sessions"
_SESSIONS_ARCHIVE = "sessions/archive"
_INDEX_FILE = "sessions/index.json"
_LOCK_KEY = "sessions_index"


def ensure_session_dirs(lattice_dir: Path) -> None:
    """Create sessions/ and sessions/archive/ if they don't exist."""
    (lattice_dir / _SESSIONS_DIR).mkdir(parents=True, exist_ok=True)
    (lattice_dir / _SESSIONS_ARCHIVE).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Index operations (always called under lock)
# ---------------------------------------------------------------------------


def _read_index(lattice_dir: Path) -> dict:
    """Read the session index.  Returns a default if it doesn't exist."""
    path = lattice_dir / _INDEX_FILE
    if not path.exists():
        return {"serial_counters": {}, "active_sessions": {}}
    return json.loads(path.read_text())


def _write_index(lattice_dir: Path, index: dict) -> None:
    """Write the session index atomically."""
    path = lattice_dir / _INDEX_FILE
    content = json.dumps(index, sort_keys=True, indent=2) + "\n"
    atomic_write(path, content)


def _next_serial(index: dict, base_name: str) -> int:
    """Get the next serial for a base name and increment the counter."""
    counters = index.setdefault("serial_counters", {})
    current = counters.get(base_name, 0)
    next_val = current + 1
    counters[base_name] = next_val
    return next_val


# ---------------------------------------------------------------------------
# Auto-generated names
# ---------------------------------------------------------------------------

# Word list for auto-generated names when no name is provided.
_AUTO_NAMES = [
    "Aether", "Beacon", "Cipher", "Drift", "Echo",
    "Flint", "Grove", "Helix", "Iris", "Jade",
    "Kite", "Lumen", "Mote", "Nexus", "Onyx",
    "Pulse", "Quill", "Rune", "Shard", "Thorn",
    "Umbra", "Vault", "Wren", "Xenon", "Yarrow", "Zephyr",
]


def _auto_name(index: dict) -> str:
    """Pick an auto-generated base name with the lowest serial count."""
    counters = index.get("serial_counters", {})
    # Pick the name from the word list with the fewest prior uses
    best = min(_AUTO_NAMES, key=lambda n: counters.get(n, 0))
    return best


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def create_session(
    lattice_dir: Path,
    *,
    base_name: str | None = None,
    agent_type: str | None = None,
    model: str,
    framework: str | None = None,
    prompt: str | None = None,
    parent: str | None = None,
    extra: dict | None = None,
) -> ActorIdentity:
    """Register a new session.

    If *base_name* is None:
        - If *agent_type* is provided, uses it as the base name (e.g., "advance")
        - Otherwise, auto-generates a name from the word list.

    Returns the fully constructed ``ActorIdentity`` with assigned serial.

    Raises:
        ValueError: If validation fails (bad name, missing required fields).
    """
    ensure_session_dirs(lattice_dir)
    locks_dir = lattice_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)

    # Determine base name
    if base_name is None:
        if agent_type is not None:
            base_name = agent_type.capitalize()
        # else: will be auto-generated under lock after reading index

    # Validate base name if provided
    if base_name is not None:
        err = validate_base_name(base_name)
        if err:
            raise ValueError(err)

    # Validate required fields
    err = validate_session_creation(model=model, framework=framework)
    if err:
        raise ValueError(err)

    # Generate session ULID
    session_id = generate_session_id()

    with lattice_lock(locks_dir, _LOCK_KEY):
        index = _read_index(lattice_dir)

        # Auto-generate name if still not set
        if base_name is None:
            base_name = _auto_name(index)

        # Assign serial
        serial = _next_serial(index, base_name)
        disambiguated = f"{base_name}-{serial}"

        # Check no active session with this exact disambiguated name
        active = index.setdefault("active_sessions", {})
        if disambiguated in active:
            raise ValueError(
                f"Session '{disambiguated}' is already active. "
                "This should not happen — serial counter may be corrupted."
            )

        # Build identity
        identity = ActorIdentity(
            name=disambiguated,
            base_name=base_name,
            serial=serial,
            session=session_id,
            model=model,
            framework=framework,
            agent_type=agent_type,
            prompt=prompt,
            parent=parent,
            extra=extra or {},
        )

        # Write session file
        now = utc_now()
        session_data = identity.to_dict()
        session_data["started_at"] = now
        session_data["last_active"] = now
        session_data["status"] = "active"

        session_path = lattice_dir / _SESSIONS_DIR / f"{disambiguated}.json"
        atomic_write(session_path, json.dumps(session_data, sort_keys=True, indent=2) + "\n")

        # Update index
        active[disambiguated] = session_id
        _write_index(lattice_dir, index)

    return identity


def resolve_session(lattice_dir: Path, name: str) -> dict | None:
    """Read an active session by disambiguated name.  Returns None if not found."""
    path = lattice_dir / _SESSIONS_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def touch_session(lattice_dir: Path, name: str) -> bool:
    """Update ``last_active`` on a session.  Returns False if session not found."""
    path = lattice_dir / _SESSIONS_DIR / f"{name}.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    data["last_active"] = utc_now()
    atomic_write(path, json.dumps(data, sort_keys=True, indent=2) + "\n")
    return True


def end_session(
    lattice_dir: Path,
    name: str,
    *,
    reason: str | None = None,
) -> bool:
    """End an active session and archive it.

    Returns True on success, False if session not found.
    """
    ensure_session_dirs(lattice_dir)
    locks_dir = lattice_dir / "locks"

    session_path = lattice_dir / _SESSIONS_DIR / f"{name}.json"
    if not session_path.exists():
        return False

    with lattice_lock(locks_dir, _LOCK_KEY):
        # Re-check under lock
        if not session_path.exists():
            return False

        data = json.loads(session_path.read_text())
        data["status"] = "ended"
        data["ended_at"] = utc_now()
        if reason:
            data["end_reason"] = reason

        # Get session ID for archive filename
        session_id = data.get("session", "unknown")

        # Archive the session file
        archive_path = lattice_dir / _SESSIONS_ARCHIVE / f"{name}_{session_id}.json"
        atomic_write(archive_path, json.dumps(data, sort_keys=True, indent=2) + "\n")

        # Remove active session file
        session_path.unlink()

        # Update index
        index = _read_index(lattice_dir)
        active = index.get("active_sessions", {})
        active.pop(name, None)
        _write_index(lattice_dir, index)

    return True


def list_sessions(lattice_dir: Path) -> list[dict]:
    """List all active sessions."""
    sessions_dir = lattice_dir / _SESSIONS_DIR
    if not sessions_dir.is_dir():
        return []
    results = []
    for path in sorted(sessions_dir.iterdir()):
        if path.suffix == ".json" and path.name != "index.json":
            results.append(json.loads(path.read_text()))
    return results
