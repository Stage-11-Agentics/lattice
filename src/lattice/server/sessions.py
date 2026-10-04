"""Dashboard sessions: ``web_sessions.json`` (SPEC §10, G-7).

A login mints a session secret (32 random bytes, base64url), sent to the
browser as the ``lattice_session`` cookie. At rest the file holds only its
SHA-256, with the token it derives from and its lifetime::

    {"sessions": [{"sha256", "token_id", "created_at", "expires_at"}]}

The file is mode 0600 (``atomic_write`` creates through ``mkstemp``). Every
write (create, delete) runs under ``<root>/admin.lock``, re-reads the file
there, and prunes sessions that expired or whose token is revoked or gone.

The running server re-reads the file whenever its
``(st_mtime_ns, st_ctime_ns, st_size, st_ino, st_mode)`` changes. Any stat,
open, or parse failure clears every cached session, so a broken file
authenticates nobody until it reads cleanly again (fail closed). A session is
valid only while it has not expired and its token exists and is not revoked,
checked on every request, so revocation ends it at once (AC-13).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lattice.core.errors import OpError
from lattice.server.config import WEB_SESSIONS_JSON
from lattice.server.log import describe_error
from lattice.server.tokens import TokenRecord, TokenStore
from lattice.storage.fs import atomic_write

COOKIE_NAME = "lattice_session"
SESSION_DAYS = 30
SESSION_SECONDS = SESSION_DAYS * 24 * 60 * 60
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(UTC)


def _format(ts: datetime) -> str:
    return ts.strftime(_TS_FORMAT)


def _expired(expires_at: Any, now: datetime) -> bool:
    try:
        return datetime.strptime(expires_at, _TS_FORMAT).replace(tzinfo=UTC) <= now
    except (TypeError, ValueError):
        return True  # an unreadable expiry is an expired session


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def unauthenticated() -> OpError:
    return OpError("UNAUTHENTICATED", "missing, invalid, expired, or revoked session")


@dataclass(frozen=True)
class Session:
    sha256: str
    token_id: str
    created_at: str
    expires_at: str

    @classmethod
    def from_json(cls, raw: dict) -> Session:
        return cls(
            sha256=str(raw["sha256"]),
            token_id=str(raw["token_id"]),
            created_at=str(raw["created_at"]),
            expires_at=str(raw["expires_at"]),
        )

    def to_json(self) -> dict:
        return {
            "sha256": self.sha256,
            "token_id": self.token_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


def _stat_key(path: Path) -> tuple[int, ...] | None:
    st = path.stat()  # FileNotFoundError is the caller's "no sessions"
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino, st.st_mode)


def _parse(text: str) -> dict[str, Session]:
    raw = json.loads(text)
    return {s.sha256: s for s in (Session.from_json(r) for r in raw["sessions"])}


class SessionStore:
    """The running server's view of ``web_sessions.json``, and its writes."""

    def __init__(self, root: Path, tokens: TokenStore, on_error: Any = None) -> None:
        self.root = Path(root)
        self.tokens = tokens
        self.on_error = on_error
        self._key: tuple[int, ...] | None | str = "unread"
        self._by_hash: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.root / WEB_SESSIONS_JSON

    # -- reads ----------------------------------------------------------------

    def refresh(self) -> None:
        with self._lock:
            try:
                key = _stat_key(self.path)
            except FileNotFoundError:
                self._by_hash, self._key = {}, None
                return
            except OSError as exc:
                self._fail(exc)
                return
            if key == self._key:
                return
            try:
                sessions = _parse(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._by_hash, self._key = {}, None
                return
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self._fail(exc)
                self._key = key  # retried once the file changes again
                return
            self._by_hash, self._key = sessions, key

    def _fail(self, exc: BaseException) -> None:
        self._by_hash = {}
        self._key = "failed"
        if self.on_error:
            self.on_error(error=describe_error(exc))

    def authenticate(self, cookie: str | None) -> tuple[Session, TokenRecord]:
        """The session a cookie value names and its live token, else ``UNAUTHENTICATED``."""
        if not cookie or not _SECRET_RE.fullmatch(cookie):
            raise unauthenticated()
        self.refresh()
        session = self._by_hash.get(hash_secret(cookie))
        if session is None or _expired(session.expires_at, _now()):
            raise unauthenticated()
        token = self.tokens.get(session.token_id)
        if token is None or token.revoked_at is not None:
            raise unauthenticated()
        return session, token

    # -- writes (under admin.lock, re-reading the file there) -----------------

    def _read_for_write(self) -> dict[str, Session]:
        try:
            return _parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # A broken file already authenticates nobody; rewriting it from
            # nothing keeps every old session dead and lets new logins work.
            if self.on_error:
                self.on_error(error=describe_error(exc))
            return {}

    def _live(self, sessions: dict[str, Session]) -> dict[str, Session]:
        now = _now()
        live = {}
        for digest, session in sessions.items():
            token = self.tokens.get(session.token_id)
            if token is None or token.revoked_at is not None:
                continue
            if _expired(session.expires_at, now):
                continue
            live[digest] = session
        return live

    def _write(self, sessions: dict[str, Session]) -> None:
        body = {"sessions": [s.to_json() for s in sessions.values()]}
        atomic_write(self.path, json.dumps(body, sort_keys=True, indent=2) + "\n")
        self.refresh()

    def create(self, token: TokenRecord) -> str:
        """A new session for *token*; returns the cookie value (the only copy)."""
        from lattice.server import admin

        if token.filing_only:
            raise OpError(
                "TOKEN_RESTRICTED", "filing-only tokens cannot create dashboard sessions"
            )

        secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        now = _now()
        session = Session(
            sha256=hash_secret(secret),
            token_id=token.id,
            created_at=_format(now),
            expires_at=_format(now + timedelta(days=SESSION_DAYS)),
        )
        with admin.admin_lock(self.root):
            sessions = self._live(self._read_for_write())
            sessions[session.sha256] = session
            self._write(sessions)
        return secret

    def delete(self, session: Session) -> None:
        from lattice.server import admin

        with admin.admin_lock(self.root):
            sessions = self._live(self._read_for_write())
            sessions.pop(session.sha256, None)
            self._write(sessions)
