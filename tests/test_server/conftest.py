"""Server test fixtures: a root with two projects, tokens, and a running server."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

import tests.test_server.server_ops  # noqa: F401 - registers the xtest.* operations
from lattice.server import tokens
from lattice.server.testing import ServerHandle, make_root, running_server


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return make_root(tmp_path, projects={"alpha": {"code": "ALP"}, "beta": {"code": "BET"}})


def mint(root: Path, *, user: str = "human:alice", machine: str = "laptop", **kw) -> str:
    """A token string for *user* (default: every project, default actors)."""
    if "projects" not in kw and "all_projects" not in kw:
        kw["all_projects"] = True
    return tokens.create_token(root, user=user, machine=machine, **kw)["token"]


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root)


@pytest.fixture()
def server(root: Path) -> Iterator[ServerHandle]:
    with running_server(root) as handle:
        yield handle


def create_task(server: ServerHandle, token: str, slug: str = "alpha", **params) -> dict:
    status, _, body = server.op(slug, "task.create", {"title": "t", **params}, token=token)
    assert status == 200, body
    return body["data"]["result"]["task"]


def tree_hash(base: Path, *, exclude: Path | None = None) -> dict[str, str]:
    """Path -> sha256 of every regular file under *base* (skipping *exclude*)."""
    out = {}
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            path = Path(dirpath) / name
            if exclude is not None and path.is_relative_to(exclude):
                continue
            out[str(path.relative_to(base))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def board_hash(root: Path, slug: str) -> dict[str, str]:
    """Hashes of a project's durable board files (hosted/, locks/ and temp files excluded)."""
    board = root / "projects" / slug / ".lattice"
    return {
        k: v
        for k, v in tree_hash(board).items()
        if not k.startswith(("hosted/", "locks/")) and ".tmp." not in k
    }
