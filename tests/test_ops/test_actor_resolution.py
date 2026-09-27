"""SPEC §3.7 inside ``execute``: resolve, permission identity, authorize, and
only then touch the session, under the ``sessions_index`` lock.

The ``authorize`` hook is the seam H-9's server supplies; locally it is
``None`` and step 3 is skipped.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

from lattice.ops import Caller, OpError, execute, permission_identity, resolve_actor
from lattice.storage.sessions import create_session

OP_ID = "op_01J9ZABCDEFGHJKMNPQRSTVWXY"


def _caller(**kwargs) -> Caller:  # noqa: ANN003
    return Caller(origin={"op_id": OP_ID}, **kwargs)


@pytest.fixture()
def lattice_dir(initialized_root: Path) -> Path:
    return initialized_root / ".lattice"


@pytest.fixture()
def session(lattice_dir: Path) -> Path:
    create_session(lattice_dir, base_name="Argus", model="claude", framework="claude-code")
    return lattice_dir / "sessions" / "Argus-1.json"


def _files(lattice_dir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(lattice_dir)): p.read_bytes()
        for p in sorted(lattice_dir.rglob("*"))
        if p.is_file() and p.parent.name != "locks"
    }


class Refuse:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, identity: str, caller: Caller) -> None:
        self.seen.append(identity)
        raise OpError("ACTOR_NOT_PERMITTED", f"actor {identity} is not permitted")


class TestPermissionIdentity:
    def test_string_actor_is_itself(self) -> None:
        assert permission_identity("human:alice") == "human:alice"

    def test_session_actor_is_agent_base_name(self) -> None:
        actor = {
            "name": "Argus-3",
            "base_name": "Argus",
            "serial": 3,
            "session": "s",
            "model": "m",
        }
        assert permission_identity(actor) == "agent:Argus"


class TestAuthorizeSeam:
    def test_session_actor_refused_before_touch(self, lattice_dir: Path, session: Path) -> None:
        before = _files(lattice_dir)
        refuse = Refuse()
        with pytest.raises(OpError) as exc:
            execute(
                lattice_dir,
                "task.create",
                {"title": "x"},
                _caller(actor_name="Argus-1"),
                run_hooks=False,
                authorize=refuse,
            )
        assert exc.value.code == "ACTOR_NOT_PERMITTED"
        assert refuse.seen == ["agent:Argus"]
        assert _files(lattice_dir) == before  # not even last_active moved

    def test_string_actor_refused_writes_nothing(self, lattice_dir: Path) -> None:
        before = _files(lattice_dir)
        refuse = Refuse()
        with pytest.raises(OpError):
            execute(
                lattice_dir,
                "resource.create",
                {"name": "gpu"},
                _caller(actor="human:bob"),
                run_hooks=False,
                authorize=refuse,
            )
        assert refuse.seen == ["human:bob"]
        assert _files(lattice_dir) == before

    def test_permitted_actor_proceeds_and_touches(self, lattice_dir: Path, session: Path) -> None:
        seen: list[str] = []
        before = json.loads(session.read_text())["last_active"]
        execute(
            lattice_dir,
            "task.create",
            {"title": "x"},
            _caller(actor_name="Argus-1"),
            run_hooks=False,
            authorize=lambda identity, caller: seen.append(identity),
        )
        assert seen == ["agent:Argus"]
        assert json.loads(session.read_text())["last_active"] >= before

    def test_no_actor_ops_skip_authorize(self, lattice_dir: Path) -> None:
        refuse = Refuse()
        execute(
            lattice_dir,
            "session.start",
            {"name": "Nova", "model": "human"},
            _caller(),
            run_hooks=False,
            authorize=refuse,
        )
        assert refuse.seen == []

    def test_unresolvable_actor_never_reaches_authorize(self, lattice_dir: Path) -> None:
        refuse = Refuse()
        with pytest.raises(OpError) as exc:
            execute(
                lattice_dir,
                "task.create",
                {"title": "x"},
                _caller(actor_name="Ghost-1"),
                run_hooks=False,
                authorize=refuse,
            )
        assert exc.value.code == "SESSION_NOT_FOUND"
        assert refuse.seen == []


class TestResolveActor:
    def test_session_wins_over_actor(self, lattice_dir: Path, session: Path) -> None:
        actor = resolve_actor(lattice_dir, Caller(actor="human:x", actor_name="Argus-1"))
        assert actor["name"] == "Argus-1"

    def test_resolution_writes_nothing(self, lattice_dir: Path, session: Path) -> None:
        before = _files(lattice_dir)
        resolve_actor(lattice_dir, Caller(actor_name="Argus-1"))
        assert _files(lattice_dir) == before


class TestLockedTouch:
    def test_touch_holds_sessions_index_lock(
        self, lattice_dir: Path, session: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import lattice.storage.sessions as sessions

        log: list[str] = []
        real_lock = sessions.lattice_lock
        real_write = sessions.atomic_write

        @contextlib.contextmanager
        def recording_lock(locks_dir, key, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            log.append(f"lock {key}")
            with real_lock(locks_dir, key, *args, **kwargs):
                yield
            log.append(f"unlock {key}")

        def recording_write(path, content):  # noqa: ANN001, ANN202
            log.append(f"write {Path(path).name}")
            return real_write(path, content)

        monkeypatch.setattr(sessions, "lattice_lock", recording_lock)
        monkeypatch.setattr(sessions, "atomic_write", recording_write)
        assert sessions.touch_session(lattice_dir, "Argus-1")
        assert log == ["lock sessions_index", "write Argus-1.json", "unlock sessions_index"]

    def test_touch_after_end_does_not_resurrect(self, lattice_dir: Path, session: Path) -> None:
        from lattice.storage.sessions import end_session, touch_session

        assert end_session(lattice_dir, "Argus-1")
        assert not touch_session(lattice_dir, "Argus-1")
        assert not session.exists()
