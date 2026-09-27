"""SPEC §3.5: the local-only maintenance commands refuse a hosted checkout.

No checkout is hosted until H-11 adds binding and routing, so the hosted
predicate here is a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.boards import LOCAL_ONLY_COMMANDS, check_local_only, hosted_binding
from lattice.ops import OpError


def bound(start: Path) -> str:
    return "home/proj"


def local(start: Path) -> None:
    return None


def test_the_list() -> None:
    assert LOCAL_ONLY_COMMANDS == (
        "init",
        "demo init",
        "rebuild",
        "doctor --fix",
        "backfill-ids",
        "migrate needs-human",
    )


@pytest.mark.parametrize("command", [c for c in LOCAL_ONLY_COMMANDS if c != "init"])
def test_maintenance_refused_on_hosted_checkout(tmp_path: Path, command: str) -> None:
    with pytest.raises(OpError) as exc:
        check_local_only(command, tmp_path, binding_of=bound)
    assert exc.value.code == "LOCAL_ONLY"
    assert exc.value.http_status == 400
    assert exc.value.details == {"command": command}
    assert f"'lattice {command}'" in exc.value.message
    assert "'home/proj'" in exc.value.message
    assert "lattice server project unload <slug>" in exc.value.message
    assert "--offline-maintenance" in exc.value.message


def test_init_has_its_own_message(tmp_path: Path) -> None:
    with pytest.raises(OpError) as exc:
        check_local_only("init", tmp_path, binding_of=bound)
    assert exc.value.code == "LOCAL_ONLY"
    assert exc.value.message == (
        "This checkout is bound to 'home/proj'; its board lives on the server. "
        "For a separate local board, work in a checkout without .lattice-remote.json."
    )


@pytest.mark.parametrize("command", LOCAL_ONLY_COMMANDS)
def test_local_checkout_allowed(tmp_path: Path, command: str) -> None:
    check_local_only(command, tmp_path, binding_of=local)
    check_local_only(command, tmp_path)  # the default predicate: nothing is hosted yet


def test_predicate_sees_the_start_directory(tmp_path: Path) -> None:
    seen: list[Path] = []
    check_local_only("rebuild", tmp_path, binding_of=lambda p: seen.append(p))
    assert seen == [tmp_path]
    assert hosted_binding(tmp_path) is None


def test_other_commands_are_not_local_only(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        check_local_only("doctor", tmp_path, binding_of=bound)
