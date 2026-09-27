"""``resource.*`` and ``session.*`` operations called directly (no CLI).

Covers SPEC §3.1's name checks at the operation level (G-6's declared change:
resource and session names that are not one safe path component are
refused), ``resource.acquire`` as one non-blocking attempt, the resource
fields of ``OpResult``, and ``origin`` in session files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError

UNSAFE = ["../x", "a/b", "a\\b", ".", "..", "", "x" * 129, "bad\x00name", "tab\tname", "del\x7f"]
A = Caller(actor="agent:a")
B = Caller(actor="agent:b")


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _tree(board: LocalBoard) -> list[str]:
    return sorted(str(p.relative_to(board.lattice_dir)) for p in board.lattice_dir.rglob("*"))


class TestNameChecks:
    @pytest.mark.parametrize("name", UNSAFE)
    @pytest.mark.parametrize(
        "op", ["resource.create", "resource.acquire", "resource.release", "resource.heartbeat"]
    )
    def test_resource_names(self, board: LocalBoard, op: str, name: str) -> None:
        before = _tree(board)
        with pytest.raises(OpError) as exc:
            board.execute(op, {"name": name}, A)
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details == {"reason": "UNSAFE_NAME", "param": "resource name"}
        assert _tree(board) == before

    def test_resource_name_checked_before_actor(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("resource.create", {"name": "../x"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"

    @pytest.mark.parametrize("name", [n for n in UNSAFE if n])
    def test_session_end_names(self, board: LocalBoard, name: str) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("session.end", {"name": name}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["reason"] == "UNSAFE_NAME"

    @pytest.mark.parametrize("name", [".", "..", "x" * 129, "bad\x00name", "del\x7f"])
    def test_session_start_names(self, board: LocalBoard, name: str) -> None:
        before = _tree(board)
        with pytest.raises(OpError) as exc:
            board.execute("session.start", {"name": name, "model": "human"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert _tree(board) == before

    def test_session_start_derived_name_checked(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("session.start", {"agent_type": "..", "model": "human"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"

    def test_session_start_keeps_todays_message_for_slashes(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("session.start", {"name": "Bad/Name", "model": "human"}, Caller())
        assert exc.value.message == (
            "Invalid name 'Bad/Name': must not contain whitespace or slashes."
        )

    @pytest.mark.parametrize("name", ["../x", "a/b", ".."])
    def test_actor_name_checked(self, board: LocalBoard, name: str) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("resource.create", {"name": "ok"}, Caller(actor_name=name))
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["param"] == "session name"


class TestResourceOps:
    def test_results_carry_resource_id_and_name(self, board: LocalBoard) -> None:
        created = board.execute("resource.create", {"name": "gpu"}, A)
        resource_id = created.value["id"]
        for op in ("resource.acquire", "resource.heartbeat", "resource.release"):
            result = board.execute(op, {"name": "gpu"}, A)
            assert (result.resource_id, result.resource_name) == (resource_id, "gpu")
            assert result.events and all(e["resource_id"] == resource_id for e in result.events)
        by_id = board.execute("resource.acquire", {"name": resource_id}, A)
        assert by_id.resource_name == "gpu"

    def test_acquire_is_one_attempt(self, board: LocalBoard) -> None:
        board.execute("resource.create", {"name": "gpu"}, A)
        board.execute("resource.acquire", {"name": "gpu"}, A)
        with pytest.raises(OpError) as exc:
            board.execute("resource.acquire", {"name": "gpu"}, B)
        assert exc.value.code == "RESOURCE_HELD"
        assert exc.value.message.startswith("Resource 'gpu' is not available. Held by agent:a")
        assert [h["actor"] for h in exc.value.details["resource"]["holders"]] == ["agent:a"]

    def test_acquire_has_no_wait_param(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("resource.acquire", {"name": "gpu", "wait": True}, A)
        assert exc.value.details["reason"] == "UNKNOWN_PARAM"

    def test_already_holding_extends(self, board: LocalBoard) -> None:
        board.execute("resource.create", {"name": "gpu"}, A)
        board.execute("resource.acquire", {"name": "gpu"}, A)
        again = board.execute("resource.acquire", {"name": "gpu"}, A)
        assert [e["type"] for e in again.events] == ["resource_heartbeat"]

    def test_auto_create_events_are_returned(self, board: LocalBoard) -> None:
        config = json.loads((board.lattice_dir / "config.json").read_text())
        config["resources"] = {"db": {"ttl_seconds": 60}}
        (board.lattice_dir / "config.json").write_text(json.dumps(config))
        result = board.execute("resource.acquire", {"name": "db"}, A)
        assert [e["type"] for e in result.events] == ["resource_created", "resource_acquired"]

    def test_release_not_held(self, board: LocalBoard) -> None:
        board.execute("resource.create", {"name": "gpu"}, A)
        with pytest.raises(OpError) as exc:
            board.execute("resource.release", {"name": "gpu"}, B)
        assert exc.value.code == "NOT_HELD"
        assert exc.value.http_status == 409


class TestSessionOps:
    def test_start_and_end_record_origin(self, board: LocalBoard) -> None:
        started = board.execute("session.start", {"name": "Argus", "model": "human"}, Caller())
        assert started.value["name"] == "Argus-1"
        path = board.lattice_dir / "sessions" / "Argus-1.json"
        origin = json.loads(path.read_text())["origin"]
        assert origin["op"] == "session.start"
        assert origin["op_id"].startswith("op_")

        board.execute("session.end", {"name": "Argus-1", "reason": "done"}, Caller())
        archived = next((board.lattice_dir / "sessions" / "archive").glob("Argus-1_*.json"))
        record = json.loads(archived.read_text())
        assert record["origin"]["op"] == "session.end"
        assert record["end_reason"] == "done"
        assert not path.exists()

    def test_each_start_allocates_a_new_serial(self, board: LocalBoard) -> None:
        names = [
            board.execute("session.start", {"name": "Argus", "model": "human"}, Caller()).value[
                "name"
            ]
            for _ in range(3)
        ]
        assert names == ["Argus-1", "Argus-2", "Argus-3"]

    def test_end_unknown(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("session.end", {"name": "Ghost"}, Caller())
        assert (exc.value.code, exc.value.message) == (
            "NOT_FOUND",
            "No active session named 'Ghost'.",
        )

    def test_touch_keeps_start_origin(self, board: LocalBoard) -> None:
        board.execute("session.start", {"name": "Argus", "model": "human"}, Caller())
        path = board.lattice_dir / "sessions" / "Argus-1.json"
        before = json.loads(path.read_text())["origin"]
        board.execute("resource.create", {"name": "gpu"}, Caller(actor_name="Argus-1"))
        assert json.loads(path.read_text())["origin"] == before
