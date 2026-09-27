"""parse_params, registration, and the path-bearing input checks (SPEC §3.1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from lattice.ops import Caller, CommonParams, OpError, operation, parse_params
from lattice.ops.base import _REGISTRY, check_op_id, check_path_component, execute


@dataclass(frozen=True, kw_only=True)
class SampleParams(CommonParams):
    task: str
    count: int = 1
    force: bool = False
    tags: tuple[str, ...] = ()
    settings: dict | None = None
    note: str | None = None

    def check(self) -> None:
        if self.note == "forbidden":
            raise OpError("VALIDATION_ERROR", "note is forbidden")


def _reason(exc: pytest.ExceptionInfo[OpError]) -> tuple[str, str | None]:
    return exc.value.details.get("reason"), exc.value.details.get("param")


class TestParseParams:
    def test_defaults_fill_and_lists_become_tuples(self) -> None:
        p = parse_params(SampleParams, {"task": "LAT-1", "tags": ["a", "b"]})
        assert p == SampleParams(task="LAT-1", tags=("a", "b"))
        assert p.count == 1 and p.model is None

    def test_instance_passes_through(self) -> None:
        p = SampleParams(task="LAT-1")
        assert parse_params(SampleParams, p) is p

    def test_missing_required(self) -> None:
        with pytest.raises(OpError) as exc:
            parse_params(SampleParams, {"count": 2}, op_name="x.y")
        assert exc.value.code == "VALIDATION_ERROR"
        assert _reason(exc) == ("MISSING_PARAM", "task")
        assert exc.value.message == "x.y: missing required parameter 'task'."

    def test_unknown_key(self) -> None:
        with pytest.raises(OpError) as exc:
            parse_params(SampleParams, {"task": "LAT-1", "colour": "red"})
        assert exc.value.code == "VALIDATION_ERROR"
        assert _reason(exc) == ("UNKNOWN_PARAM", "colour")

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("task", 5),
            ("task", None),  # required and not optional
            ("count", True),  # bool is not an int
            ("count", "3"),
            ("force", 1),  # int is not a bool
            ("force", "true"),
            ("tags", "a,b"),
            ("tags", ["a", 1]),
            ("settings", ["a"]),
            ("note", 3),
            ("model", {"a": 1}),
        ],
    )
    def test_wrong_type(self, key: str, value: object) -> None:
        obj = {"task": "LAT-1", key: value}
        with pytest.raises(OpError) as exc:
            parse_params(SampleParams, obj)
        assert exc.value.code == "VALIDATION_ERROR"
        assert _reason(exc) == ("WRONG_TYPE", key)

    def test_optional_accepts_null(self) -> None:
        assert parse_params(SampleParams, {"task": "t", "note": None}).note is None

    @pytest.mark.parametrize("obj", [None, [], "task", 3])
    def test_non_object(self, obj: object) -> None:
        with pytest.raises(OpError) as exc:
            parse_params(SampleParams, obj)
        assert exc.value.code == "VALIDATION_ERROR"

    def test_check_runs(self) -> None:
        with pytest.raises(OpError, match="note is forbidden"):
            parse_params(SampleParams, {"task": "t", "note": "forbidden"})


class TestRegistration:
    def test_rejects_bad_names(self) -> None:
        for name in ("create", "Task.create", "task.", "task.create.more", "task-x.y"):
            with pytest.raises(ValueError):
                operation(name)

    def test_rejects_unsupported_annotation(self) -> None:
        @dataclass(frozen=True)
        class BadParams:
            items: list[int]

        with pytest.raises(TypeError, match="unsupported type"):

            @operation("xtest.bad")
            class Bad:
                Params = BadParams

                def run(self, ctx, p):  # noqa: ANN001, ANN201
                    return None

    def test_rejects_unfrozen_params(self) -> None:
        @dataclass
        class LooseParams:
            task: str

        with pytest.raises(TypeError, match="frozen"):

            @operation("xtest.loose")
            class Loose:
                Params = LooseParams

                def run(self, ctx, p):  # noqa: ANN001, ANN201
                    return None

    def test_rejects_duplicate_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("lattice.ops.base._REGISTRY", dict(_REGISTRY))

        @operation("xtest.dup")
        class First:
            Params = SampleParams

            def run(self, ctx, p):  # noqa: ANN001, ANN201
                return None

        with pytest.raises(ValueError, match="already registered"):

            @operation("xtest.dup")
            class Second:
                Params = SampleParams

                def run(self, ctx, p):  # noqa: ANN001, ANN201
                    return None


OK_OP_ID = "op_01J9ZABCDEFGHJKMNPQRSTVWXY"


class TestOpId:
    def test_valid(self) -> None:
        check_op_id(OK_OP_ID)

    @pytest.mark.parametrize(
        "op_id",
        [
            None,
            "",
            "op_../../x",
            "op_01j9zabcdefghjkmnpqrstvwxy",  # lowercase
            "op_01J9ZABCDEFGHJKMNPQRSTVWX",  # 25 characters
            "op_01J9ZABCDEFGHJKMNPQRSTVWXYZ",  # 27 characters
            "01J9ZABCDEFGHJKMNPQRSTVWXY",  # no prefix
            "ev_01J9ZABCDEFGHJKMNPQRSTVWXY",
            "op_01J9ZABCDEFGHIKMNPQRSTVWXY",  # I is not Crockford
            "op_01J9ZABCDEFGHJKLNPQRSTVWXY",  # L
            "op_01J9ZABCDEFGHJKMNOQRSTVWXY",  # O
            "op_01J9ZABCDEFGHJKMNPQRSTUWXY",  # U
            "op_01J9ZABCDEFGHJKMNPQRSTVWXY\n",
            12345,
        ],
    )
    def test_malformed(self, op_id: object) -> None:
        with pytest.raises(OpError) as exc:
            check_op_id(op_id)
        assert exc.value.code == "VALIDATION_ERROR"

    def test_execute_refuses_before_anything_runs(self, initialized_root) -> None:  # noqa: ANN001
        lattice_dir = initialized_root / ".lattice"
        before = sorted(p.relative_to(lattice_dir) for p in lattice_dir.rglob("*"))
        with pytest.raises(OpError) as exc:
            execute(
                lattice_dir,
                "task.create",
                {"title": "x"},
                Caller(actor="agent:t", origin={"op_id": "op_../../x"}),
                run_hooks=True,
            )
        assert exc.value.code == "VALIDATION_ERROR"
        assert sorted(p.relative_to(lattice_dir) for p in lattice_dir.rglob("*")) == before


UNSAFE_NAMES = [
    "",
    ".",
    "..",
    "../x",
    "../../tmp/x",
    "a/b",
    "a\\b",
    "a\x00b",
    "a\x1b[31mb",
    "tab\there",
    "del\x7f",
    "c1\x85",
    "x" * 129,
]


class TestPathComponents:
    @pytest.mark.parametrize("name", ["Argus-3", "a", "x" * 128, "res.name_1", "ümlaut"])
    def test_safe(self, name: str) -> None:
        check_path_component(name, "resource name")

    @pytest.mark.parametrize("name", UNSAFE_NAMES)
    def test_unsafe_resource_name(self, name: str) -> None:
        with pytest.raises(OpError) as exc:
            check_path_component(name, "resource name")
        assert exc.value.code == "VALIDATION_ERROR"

    @pytest.mark.parametrize("name", UNSAFE_NAMES)
    def test_unsafe_session_name_refused_by_execute(
        self,
        initialized_root,
        name: str,  # noqa: ANN001
    ) -> None:
        with pytest.raises(OpError) as exc:
            execute(
                initialized_root / ".lattice",
                "task.create",
                {"title": "x"},
                Caller(actor_name=name, origin={"op_id": OK_OP_ID}),
                run_hooks=True,
            )
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["reason"] == "UNSAFE_NAME"
