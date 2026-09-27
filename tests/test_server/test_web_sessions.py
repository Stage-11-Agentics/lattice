"""G-7 and AC-13 (H-13b): the web_sessions.json store fails closed and serializes
its writes under admin.lock."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.server import tokens
from lattice.server.sessions import SessionStore
from lattice.server.tokens import TokenStore


@pytest.fixture()
def store(root: Path) -> SessionStore:
    return SessionStore(root, TokenStore(root))


def _token(root: Path, store: SessionStore):
    data = tokens.create_token(root, user="human:alice", machine="m", all_projects=True)
    return store.tokens.get(data["record"]["id"])


def _refused(store: SessionStore, cookie: str) -> bool:
    try:
        store.authenticate(cookie)
    except OpError as exc:
        return exc.code == "UNAUTHENTICATED"
    return False


def test_corrupt_same_size_content_fails_closed_then_recovers(root, store) -> None:
    cookie = store.create(_token(root, store))
    assert store.authenticate(cookie)[1].user == "human:alice"
    path = store.path
    good = path.read_bytes()
    stat = path.stat()
    path.write_bytes(b"{" * len(good))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size, same mtime
    assert _refused(store, cookie)
    path.write_bytes(good)
    assert store.authenticate(cookie)[1].user == "human:alice"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_file_fails_closed(root, store) -> None:
    cookie = store.create(_token(root, store))
    os.chmod(store.path, 0)
    try:
        assert _refused(store, cookie)
    finally:
        os.chmod(store.path, 0o600)
    assert store.authenticate(cookie)


def test_mode_change_alone_triggers_a_reread(root, store, monkeypatch) -> None:
    cookie = store.create(_token(root, store))
    store.authenticate(cookie)
    reads = []
    original = Path.read_text
    monkeypatch.setattr(
        Path, "read_text", lambda self, *a, **k: reads.append(self) or original(self, *a, **k)
    )
    os.chmod(store.path, 0o640)
    store.authenticate(cookie)
    assert store.path in reads


def test_a_write_rereads_the_file_under_the_lock(root, store) -> None:
    token = _token(root, store)
    other = SessionStore(root, TokenStore(root))  # a second writer with a stale view
    first = store.create(token)
    second = other.create(token)
    assert store.authenticate(first) and store.authenticate(second)
    stored = json.loads(store.path.read_text())["sessions"]
    assert len(stored) == 2


def test_concurrent_logins_all_land(root, store) -> None:
    token = _token(root, store)
    cookies: list[str] = []
    lock = threading.Lock()

    def login() -> None:
        value = SessionStore(root, TokenStore(root)).create(token)
        with lock:
            cookies.append(value)

    threads = [threading.Thread(target=login) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cookies) == 8
    for cookie in cookies:
        assert store.authenticate(cookie)


def test_writes_prune_expired_and_revoked(root, store) -> None:
    alice = _token(root, store)
    bob_data = tokens.create_token(root, user="human:bob", machine="m", all_projects=True)
    bob = store.tokens.get(bob_data["record"]["id"])
    old = store.create(alice)
    store.create(bob)
    body = json.loads(store.path.read_text())
    for session in body["sessions"]:
        if session["token_id"] == alice.id:
            session["expires_at"] = "2000-01-01T00:00:00Z"
    store.path.write_text(json.dumps(body))
    assert _refused(store, old)
    tokens.revoke_token(root, bob.id)
    fresh = store.create(alice)
    stored = json.loads(store.path.read_text())["sessions"]
    assert [s["token_id"] for s in stored] == [alice.id]
    assert store.authenticate(fresh)


def test_malformed_cookies_are_refused_without_a_read(root, store) -> None:
    for cookie in (None, "", "x", "a" * 42, "a" * 44, "a" * 42 + "!"):
        assert _refused(store, cookie)
