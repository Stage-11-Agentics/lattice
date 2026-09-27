"""AC-12 (operations part): config authorization.

The three ``board.set_*`` operations change only their own key. An attempt
through any operation to change a workflow, review, policy, or hook key is
refused and leaves ``config.json`` byte-for-byte unchanged; where an
operation takes config keys at all (``board.set_dashboard_config``), the
refusal is ``FORBIDDEN``. The server-side part (a token, then
``lattice server project config``) is H-9's ``tests/test_server/test_config_auth.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError, registered_operations
from lattice.storage.board_config import OPERATION_CONFIG_KEYS, update_config_key

# A workflow, review, policy, and hook key, each with a plausible new value.
ADMIN_KEYS = {
    "workflow": {"statuses": ["open"], "transitions": {}},
    "review_mode": "triple",
    "plan_review_mode": "inline",
    "auto_code_review_on_transition": True,
    "completion_policies": {"done": {"require_roles": []}},
    "hooks": {"post_event": "touch /tmp/pwned"},
}


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _config_bytes(board: LocalBoard) -> bytes:
    return (board.lattice_dir / "config.json").read_bytes()


def _config(board: LocalBoard) -> dict:
    return json.loads(_config_bytes(board))


def _without(config: dict, key: str) -> dict:
    return {k: v for k, v in config.items() if k != key}


class TestOwnKeyOnly:
    def test_set_project_code(self, board: LocalBoard) -> None:
        before = _config(board)
        result = board.execute("board.set_project_code", {"code": "abc"}, Caller())
        after = _config(board)
        assert after["project_code"] == "ABC"
        assert _without(after, "project_code") == _without(before, "project_code")
        assert result.value == {"project_code": "ABC", "previous": None}

    def test_set_subproject_code(self, board: LocalBoard) -> None:
        board.execute("board.set_project_code", {"code": "ABC"}, Caller())
        before = _config(board)
        board.execute("board.set_subproject_code", {"code": "sub"}, Caller())
        after = _config(board)
        assert after["subproject_code"] == "SUB"
        assert _without(after, "subproject_code") == _without(before, "subproject_code")

    def test_set_dashboard_config(self, board: LocalBoard) -> None:
        before = _config(board)
        result = board.execute(
            "board.set_dashboard_config",
            {"settings": {"theme": "dark", "lane_colors": {"done": "#0f0"}}},
            Caller(),
        )
        after = _config(board)
        assert after["dashboard"] == {"theme": "dark", "lane_colors": {"done": "#0f0"}}
        assert result.value == after["dashboard"]
        assert _without(after, "dashboard") == _without(before, "dashboard")

    def test_dashboard_null_removes_and_empty_drops_the_key(self, board: LocalBoard) -> None:
        board.execute("board.set_dashboard_config", {"settings": {"theme": "dark"}}, Caller())
        board.execute("board.set_dashboard_config", {"settings": {"theme": None}}, Caller())
        assert "dashboard" not in _config(board)

    def test_board_ops_take_no_actor(self, board: LocalBoard) -> None:
        for name in ("board.set_project_code", "board.set_subproject_code"):
            assert getattr(registered_operations()[name], "no_actor", False)
        assert getattr(registered_operations()["board.set_dashboard_config"], "no_actor", False)


class TestAdminKeysRefused:
    @pytest.mark.parametrize("key", sorted(ADMIN_KEYS))
    def test_dashboard_settings_naming_config_key_forbidden(
        self, board: LocalBoard, key: str
    ) -> None:
        before = _config_bytes(board)
        with pytest.raises(OpError) as exc:
            board.execute(
                "board.set_dashboard_config", {"settings": {key: ADMIN_KEYS[key]}}, Caller()
            )
        assert exc.value.code == "FORBIDDEN"
        assert exc.value.http_status == 403
        assert exc.value.details == {"key": key}
        assert _config_bytes(board) == before

    def test_forbidden_even_beside_valid_settings(self, board: LocalBoard) -> None:
        before = _config_bytes(board)
        with pytest.raises(OpError) as exc:
            board.execute(
                "board.set_dashboard_config",
                {"settings": {"theme": "dark", "hooks": {"post_event": "x"}}},
                Caller(),
            )
        assert exc.value.code == "FORBIDDEN"
        assert _config_bytes(board) == before

    def test_key_present_only_in_this_boards_config_forbidden(self, board: LocalBoard) -> None:
        config = _config(board)
        config["custom_policy"] = {"x": 1}
        (board.lattice_dir / "config.json").write_text(json.dumps(config))
        with pytest.raises(OpError) as exc:
            board.execute(
                "board.set_dashboard_config", {"settings": {"custom_policy": {}}}, Caller()
            )
        assert exc.value.code == "FORBIDDEN"

    def test_unknown_non_config_key_keeps_the_posts_error(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("board.set_dashboard_config", {"settings": {"unknown_key": 1}}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.message == "Unknown keys: unknown_key"

    @pytest.mark.parametrize("key", sorted(ADMIN_KEYS))
    def test_writer_refuses_admin_keys(self, board: LocalBoard, key: str) -> None:
        before = _config_bytes(board)
        called = []
        with pytest.raises(OpError) as exc:
            update_config_key(board.lattice_dir, key, lambda c: called.append(c) or "x")
        assert exc.value.code == "FORBIDDEN"
        assert called == []  # refused before the config was even read
        assert _config_bytes(board) == before

    def test_writer_keys_are_exactly_the_three(self) -> None:
        assert frozenset({"project_code", "subproject_code", "dashboard"}) == OPERATION_CONFIG_KEYS

    @pytest.mark.parametrize("key", sorted(ADMIN_KEYS))
    def test_no_operation_accepts_a_config_key(self, board: LocalBoard, key: str) -> None:
        """Every registered operation refuses a config key passed as a parameter."""
        before = _config_bytes(board)
        for name in registered_operations():
            with pytest.raises(OpError):
                board.execute(name, {key: ADMIN_KEYS[key]}, Caller(actor="human:t"))
            assert _config_bytes(board) == before, name


class TestRules:
    def test_project_code_conflict_without_force(self, board: LocalBoard) -> None:
        board.execute("board.set_project_code", {"code": "ABC"}, Caller())
        before = _config_bytes(board)
        with pytest.raises(OpError) as exc:
            board.execute("board.set_project_code", {"code": "XYZ"}, Caller())
        assert exc.value.code == "CONFLICT"
        assert _config_bytes(board) == before
        result = board.execute("board.set_project_code", {"code": "ABC"}, Caller())
        assert result.idempotent
        result = board.execute("board.set_project_code", {"code": "XYZ", "force": True}, Caller())
        assert result.value == {"project_code": "XYZ", "previous": "ABC"}

    def test_invalid_code(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("board.set_project_code", {"code": "bad code!"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.message.startswith("Invalid project code: 'BAD CODE!'.")

    def test_subproject_needs_project_code(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("board.set_subproject_code", {"code": "SUB"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert "without a project code" in exc.value.message

    @pytest.mark.parametrize(
        ("settings", "message"),
        [
            ({"lane_colors": ["x"]}, "'lane_colors' must be an object"),
            ({"theme": 3}, "'theme' must be a string or null"),
            ({"background_image": "file:///x"}, "'background_image' must be an http or https URL"),
            ({"heat_map_enabled": "yes"}, "'heat_map_enabled' must be a boolean"),
            (
                {"day_start_hour": 24},
                "'day_start_hour' must be an integer between 0 and 23, or null",
            ),
            ({"font_size": 5}, "'font_size' must be a number between 6 and 100, or null"),
        ],
    )
    def test_dashboard_validation(self, board: LocalBoard, settings: dict, message: str) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("board.set_dashboard_config", {"settings": settings}, Caller())
        assert (exc.value.code, exc.value.message) == ("VALIDATION_ERROR", message)
