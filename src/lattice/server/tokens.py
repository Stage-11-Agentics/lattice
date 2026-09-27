"""Tokens and the actors they may act as (SPEC §8.3).

A token string is ``lat_<token_id>_<secret>``: ``token_id`` is ``tok_`` plus a
ULID, ``secret`` is 32 random bytes in base64url (so it may itself hold ``_``
and ``-``). ``tokens.json`` (mode 0600) stores only the secret's SHA-256, and
comparison uses ``hmac.compare_digest``. A token is issued to one person
(``user``) for one machine or seat (``machine``); both are stamped into
``origin.authenticated``.

``actors`` are ``fnmatch`` patterns over actor IDs. Every token may also act
as ``agent:lattice-auto-review`` (the built-in allowance, so auto-review
works under strict tokens).

The running server re-reads ``tokens.json`` whenever its
``(st_mtime_ns, st_size, st_ino)`` differs from the last load, checked on
every request, so revocations and grants apply to the next request and two
edits within one mtime tick are never missed (``atomic_write`` replaces the
file, so each edit gets a new inode).
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from lattice.core.auto_review import AUTO_REVIEW_ACTOR
from lattice.core.errors import OpError
from lattice.core.events import utc_now
from lattice.core.ids import generate_instance_id, validate_actor
from lattice.server.config import TOKENS_JSON
from lattice.storage.fs import atomic_write

TOKEN_PREFIX = "lat_"
_TOKEN_ID_RE = re.compile(r"^tok_[0-9A-HJKMNP-TV-Z]{26}$")
_TOKEN_ID_LEN = len("tok_") + 26
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_WILDCARD = re.compile(r"[*?\[]")
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,128}$")


def new_token_id() -> str:
    return "tok_" + generate_instance_id().removeprefix("inst_")


def new_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def format_token(token_id: str, secret: str) -> str:
    return f"{TOKEN_PREFIX}{token_id}_{secret}"


def parse_token(text: str) -> tuple[str, str] | None:
    """``(token_id, secret)`` from ``lat_<token_id>_<secret>``, or ``None``.

    The token ID has a fixed length, so a secret holding ``_`` parses correctly.
    """
    if not isinstance(text, str) or not text.startswith(TOKEN_PREFIX):
        return None
    rest = text[len(TOKEN_PREFIX) :]
    token_id, sep, secret = (
        rest[:_TOKEN_ID_LEN],
        rest[_TOKEN_ID_LEN : _TOKEN_ID_LEN + 1],
        rest[_TOKEN_ID_LEN + 1 :],
    )
    if sep != "_" or not _TOKEN_ID_RE.fullmatch(token_id) or not _SECRET_RE.fullmatch(secret):
        return None
    return token_id, secret


def is_literal(pattern: str) -> bool:
    return not _WILDCARD.search(pattern)


@dataclass(frozen=True)
class TokenRecord:
    id: str
    sha256: str
    user: str
    machine: str
    actors: tuple[str, ...]
    projects: tuple[str, ...]
    created_at: str
    revoked_at: str | None = None

    @classmethod
    def from_json(cls, raw: dict) -> TokenRecord:
        return cls(
            id=raw["id"],
            sha256=raw["sha256"],
            user=raw["user"],
            machine=raw["machine"],
            actors=tuple(raw.get("actors") or ()),
            projects=tuple(raw.get("projects") or ()),
            created_at=raw.get("created_at", ""),
            revoked_at=raw.get("revoked_at"),
        )

    def to_json(self) -> dict:
        data = asdict(self)
        data["actors"] = list(self.actors)
        data["projects"] = list(self.projects)
        return data

    def public(self) -> dict:
        """Everything but the hash (``token list``, ``/v1/info``)."""
        data = self.to_json()
        data.pop("sha256")
        return data

    # -- authorization -------------------------------------------------------

    @property
    def all_projects(self) -> bool:
        return "*" in self.projects

    def permits_project(self, slug: str) -> bool:
        return self.all_projects or slug in self.projects

    def permits_actor(self, actor: str) -> bool:
        """Whether this token may act as *actor* (a string actor or a permission identity)."""
        if actor == AUTO_REVIEW_ACTOR:
            return True
        return any(fnmatch.fnmatchcase(actor, pattern) for pattern in self.actors)

    @property
    def default_actor(self) -> str | None:
        """The single non-wildcard pattern, when exactly one exists (SPEC §8.3)."""
        literals = [p for p in self.actors if is_literal(p)]
        return literals[0] if len(literals) == 1 else None

    @property
    def browser_actor(self) -> str | None:
        """A dashboard write's actor: the user if permitted, else the default actor."""
        if self.permits_actor(self.user):
            return self.user
        return self.default_actor

    def authenticated_origin(self) -> dict:
        return {"token_id": self.id, "user": self.user, "machine": self.machine}

    def not_permitted(self, actor: str) -> OpError:
        patterns = ", ".join(f"`{p}`" for p in self.actors) or "(no actors)"
        return OpError(
            "ACTOR_NOT_PERMITTED",
            f"actor `{actor}` is not permitted for token `{self.id}`; it may act as: "
            f"{patterns}. An admin can widen it with "
            f"`lattice server token grant {self.id} --actor '<pattern>'`.",
            {"actor": actor, "token_id": self.id, "patterns": list(self.actors)},
        )

    def authorize_actor(self, actor: str) -> None:
        if not self.permits_actor(actor):
            raise self.not_permitted(actor)


# ---------------------------------------------------------------------------
# The registry on disk
# ---------------------------------------------------------------------------


def _read(root: Path) -> list[TokenRecord]:
    raw = json.loads((Path(root) / TOKENS_JSON).read_text(encoding="utf-8"))
    return [TokenRecord.from_json(t) for t in raw.get("tokens", [])]


def _write(root: Path, records: list[TokenRecord]) -> None:
    body = {"tokens": [r.to_json() for r in records]}
    atomic_write(Path(root) / TOKENS_JSON, json.dumps(body, sort_keys=True, indent=2) + "\n")


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


@dataclass
class TokenStore:
    """The running server's view of ``tokens.json``, reloaded when it changes."""

    root: Path
    on_reload: Any = None
    _key: tuple[int, int, int] | None = field(default=None, init=False)
    _by_id: dict[str, TokenRecord] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def refresh(self) -> None:
        path = Path(self.root) / TOKENS_JSON
        key = _stat_key(path)
        if key == self._key:
            return
        with self._lock:
            key = _stat_key(path)
            if key == self._key:
                return
            try:
                records = _read(self.root)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                # Fail closed: an unreadable registry authenticates nobody (a
                # hand edit that revoked a token must never leave it valid),
                # until the file changes and parses again.
                self._by_id = {}
                self._key = key
                if self.on_reload:
                    self.on_reload(ok=False, error=f"{type(exc).__name__}: {exc}")
                return
            self._by_id = {r.id: r for r in records}
            self._key = key
            if self.on_reload:
                self.on_reload(ok=True, tokens=len(records))

    def authenticate(self, authorization: str | None) -> TokenRecord:
        """The token a ``Bearer`` header names, or ``UNAUTHENTICATED`` (one message for
        every failure, so the answer reveals nothing about which part was wrong)."""
        self.refresh()
        failure = OpError("UNAUTHENTICATED", "missing, invalid, or revoked credential")
        if not authorization:
            raise failure
        scheme, _, value = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            raise failure
        parsed = parse_token(value.strip())
        if parsed is None:
            raise failure
        token_id, secret = parsed
        record = self._by_id.get(token_id)
        expected = record.sha256 if record else "0" * 64
        matches = hmac.compare_digest(hash_secret(secret), expected)
        if record is None or not matches or record.revoked_at is not None:
            raise failure
        return record

    def get(self, token_id: str) -> TokenRecord | None:
        self.refresh()
        return self._by_id.get(token_id)


# ---------------------------------------------------------------------------
# Admin actions (under admin.lock)
# ---------------------------------------------------------------------------


def _check_pattern(pattern: str) -> str:
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > 128
        or any(c.isspace() or ord(c) < 0x20 for c in pattern)
    ):
        raise OpError("VALIDATION_ERROR", f"Invalid actor pattern {pattern!r}.")
    if ":" not in pattern:
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid actor pattern {pattern!r}: expected prefix:identifier "
            "(for example agent:* or human:alice).",
        )
    return pattern


def create_token(
    root: Path,
    *,
    user: str,
    machine: str,
    actors: tuple[str, ...] | list[str] = (),
    projects: tuple[str, ...] | list[str] = (),
    all_projects: bool = False,
) -> dict:
    """Mint a token. Returns ``{token, record, warning}``; the token string exists only here."""
    from lattice.server import admin

    admin.require_root(root)
    if not validate_actor(user) or not user.startswith("human:"):
        raise OpError("VALIDATION_ERROR", f"--user must be a human: actor, got {user!r}.")
    if not isinstance(machine, str) or not _LABEL_RE.fullmatch(machine):
        raise OpError("VALIDATION_ERROR", "--machine must be 1-128 printable characters.")
    patterns = tuple(_check_pattern(p) for p in actors) or (user, "agent:*")
    if all_projects and projects:
        raise OpError("VALIDATION_ERROR", "Use --project or --all-projects, not both.")
    for slug in projects:
        admin.check_slug(slug)
    scope = ("*",) if all_projects else tuple(dict.fromkeys(projects))
    token_id, secret = new_token_id(), new_secret()
    record = TokenRecord(
        id=token_id,
        sha256=hash_secret(secret),
        user=user,
        machine=machine,
        actors=tuple(dict.fromkeys(patterns)),
        projects=scope,
        created_at=utc_now(),
    )
    with admin.admin_lock(root):
        records = _read(root)
        _write(root, [*records, record])
    warning = None
    if not any(fnmatch.fnmatchcase(user, p) for p in record.actors):
        warning = (
            f"no --actor pattern matches {user}; dashboard writes with this token will "
            "not act as the user."
        )
    return {"token": format_token(token_id, secret), "record": record.public(), "warning": warning}


def list_tokens(root: Path) -> list[dict]:
    from lattice.server import admin

    admin.require_root(root)
    return [r.public() for r in _read(root)]


def _update(root: Path, token_id: str, change: Any) -> dict:
    from lattice.server import admin

    admin.require_root(root)
    with admin.admin_lock(root):
        records = _read(root)
        for i, record in enumerate(records):
            if record.id == token_id:
                records[i] = change(record)
                _write(root, records)
                return records[i].public()
    raise OpError("NOT_FOUND", f"No token {token_id}.")


def revoke_token(root: Path, token_id: str) -> dict:
    return _update(
        root, token_id, lambda r: r if r.revoked_at else replace(r, revoked_at=utc_now())
    )


def grant(
    root: Path, token_id: str, *, projects: tuple[str, ...] = (), actors: tuple[str, ...] = ()
) -> dict:
    from lattice.server import admin

    for slug in projects:
        if slug != "*":
            admin.check_slug(slug)
    for pattern in actors:
        _check_pattern(pattern)
    if not projects and not actors:
        raise OpError("VALIDATION_ERROR", "Give at least one --project or --actor.")

    def change(record: TokenRecord) -> TokenRecord:
        return replace(
            record,
            projects=tuple(dict.fromkeys((*record.projects, *projects))),
            actors=tuple(dict.fromkeys((*record.actors, *actors))),
        )

    return _update(root, token_id, change)


def ungrant(
    root: Path, token_id: str, *, projects: tuple[str, ...] = (), actors: tuple[str, ...] = ()
) -> dict:
    if not projects and not actors:
        raise OpError("VALIDATION_ERROR", "Give at least one --project or --actor.")

    def change(record: TokenRecord) -> TokenRecord:
        return replace(
            record,
            projects=tuple(p for p in record.projects if p not in projects),
            actors=tuple(a for a in record.actors if a not in actors),
        )

    return _update(root, token_id, change)
