"""Repository hygiene (G-3): no deployment-specific hostnames, tokens, or proxy secrets.

Three layers, all over the files git tracks:

1. **Built-in patterns**, always on: Lattice token shapes, private keys,
   Cloudflare Access credential values, tailnet (``*.ts.net``) names other than
   the ``example`` placeholder, ``*.cloudflareaccess.com`` team names other than
   ``example``, and IPv4 host addresses in the tailnet range ``100.64.0.0/10``
   (a range written in CIDR with a prefix of /16 or wider is documentation).
2. **Committed denylist** ``tests/hygiene_denylist.sha256``: a salt line, then one
   ``sha256(salt + string)`` per line. Every lowercased ``[a-z0-9.-]+`` token of
   every tracked text file (edge dots and hyphens trimmed) is hashed, along with
   each of its dot-suffixes, so ``x.host.example`` also hits a denylisted
   ``host.example``. The file names nothing in plain text.
3. **Private denylist** ``LATTICE_HYGIENE_DENYLIST``: when set, a path to a file
   outside the repo in either format (a salted-hash file like the committed one,
   or plain strings, one per line).

Layers 1 and 2 are what CI checks; layer 3 only adds. Findings name the file,
line, and rule, never the matched text, so a report never republishes a secret.

Add a string to the committed denylist (the string itself is never written)::

    uv run python -m tests.hygiene add 'some.private.host'
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DENYLIST_PATH = Path(__file__).resolve().parent / "hygiene_denylist.sha256"
PRIVATE_DENYLIST_ENV = "LATTICE_HYGIENE_DENYLIST"
SALT_PREFIX = "salt:"

_TAILNET_IP = re.compile(r"(?<![\d.])100\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)(?:/(\d{1,2}))?")


def _tailnet_ip(match: re.Match[str]) -> bool:
    """A host address in 100.64.0.0/10; a written range (prefix /16 or wider) is not a host."""
    second, third, fourth = (int(g) for g in match.groups()[:3])
    prefix = match.group(4)
    if prefix is not None and int(prefix) <= 16:
        return False
    return 64 <= second <= 127 and third <= 255 and fourth <= 255


def _label_not_example(match: re.Match[str]) -> bool:
    return match.group(1).lower() != "example"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    accept: object = None  # optional predicate(match) -> bool; True means "a real hit"

    def hits(self, line: str) -> bool:
        for match in self.pattern.finditer(line):
            if self.accept is None or self.accept(match):  # type: ignore[operator]
                return True
        return False


RULES: tuple[Rule, ...] = (
    Rule("lattice-token", re.compile(r"lat_tok_[A-Za-z0-9_-]{16,}")),
    Rule("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    Rule("cf-access-client-id", re.compile(r"\b[0-9a-f]{32}\.access\b")),
    Rule(
        "cf-access-client-secret",
        re.compile(r"(?i)cf[-_]access[-_]client[-_]secret[\"']?\s*[:=]\s*[\"']?[0-9a-f]{64}\b"),
    ),
    Rule(
        "cf-access-jwt",
        re.compile(
            r"(?i)(?:cf[-_]access[-_]jwt[-_]assertion|cf_authorization)[\"']?\s*[:=]\s*[\"']?"
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
        ),
    ),
    Rule(
        "tailnet-name",
        re.compile(r"(?i)(?<![a-z0-9-])(?:[a-z0-9-]+\.)*([a-z0-9-]+)\.ts\.net\b"),
        _label_not_example,
    ),
    Rule(
        "cloudflare-access-team",
        re.compile(r"(?i)(?<![a-z0-9-])([a-z0-9-]+)\.cloudflareaccess\.com\b"),
        _label_not_example,
    ),
    Rule("tailnet-ipv4", _TAILNET_IP, _tailnet_ip),
)

_TOKEN = re.compile(r"[a-z0-9.-]+")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True)
class Denylist:
    """Denylisted strings, as salted hashes (``salt`` set) or in plain text."""

    salt: str | None
    entries: frozenset[str]

    def contains(self, candidate: str) -> bool:
        if self.salt is None:
            return candidate in self.entries
        return _digest(self.salt, candidate) in self.entries

    @property
    def empty(self) -> bool:
        return not self.entries


def _digest(salt: str, value: str) -> str:
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def load_denylist(path: Path) -> Denylist:
    """Read a denylist file: ``salt:<salt>`` then hex digests, or plain strings."""
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if lines and lines[0].startswith(SALT_PREFIX):
        return Denylist(lines[0][len(SALT_PREFIX) :], frozenset(ln.lower() for ln in lines[1:]))
    return Denylist(None, frozenset(ln.lower() for ln in lines))


def tracked_files(root: Path = REPO_ROOT) -> list[str] | None:
    """Tracked paths relative to *root*, or None outside a git work tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:  # deleted in the working tree, a dangling symlink, a directory
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _candidates(line: str) -> Iterator[str]:
    for token in _TOKEN.findall(line.lower()):
        token = token.strip(".-")
        while token:
            yield token
            _, dot, rest = token.partition(".")
            token = rest if dot else ""


def scan_text(path: str, text: str, denylists: Iterable[Denylist] = ()) -> list[Finding]:
    lists = [d for d in denylists if not d.empty]
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            if rule.hits(line):
                findings.append(Finding(path, lineno, rule.name))
        for denylist in lists:
            if any(denylist.contains(c) for c in _candidates(line)):
                findings.append(Finding(path, lineno, "denylisted string"))
                break
    return findings


def active_denylists(env: dict[str, str] | None = None) -> list[Denylist]:
    env = os.environ if env is None else env
    lists = []
    if DENYLIST_PATH.exists():
        lists.append(load_denylist(DENYLIST_PATH))
    private = env.get(PRIVATE_DENYLIST_ENV)
    if private:
        lists.append(load_denylist(Path(private).expanduser()))
    return lists


def scan_repo(root: Path = REPO_ROOT, denylists: Iterable[Denylist] = ()) -> list[Finding]:
    files = tracked_files(root)
    if files is None:
        raise RuntimeError(f"{root} is not a git work tree")
    lists = list(denylists)
    findings: list[Finding] = []
    for rel in files:
        text = _read_text(root / rel)
        if text is not None:
            findings.extend(scan_text(rel, text, lists))
    return findings


def add_to_denylist(value: str, path: Path = DENYLIST_PATH) -> None:
    """Append ``sha256(salt + value)`` to the committed denylist, creating it if needed."""
    if path.exists():
        denylist = load_denylist(path)
        assert denylist.salt is not None, f"{path} is not a salted-hash file"
        salt = denylist.salt
        body = path.read_text(encoding="utf-8")
    else:
        salt = secrets.token_hex(16)
        body = (
            "# G-3 denylist: sha256(salt + string) per line. Add with:\n"
            "#   uv run python -m tests.hygiene add '<string>'\n"
            f"{SALT_PREFIX}{salt}\n"
        )
    digest = _digest(salt, value.strip().lower())
    if digest not in body:
        body = body.rstrip("\n") + "\n" + digest + "\n"
    path.write_text(body, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "add":
        for value in argv[1:]:
            add_to_denylist(value)
        print(f"added {len(argv) - 1} hash(es) to {DENYLIST_PATH.name}")
        return 0
    if argv in ([], ["scan"]):
        findings = scan_repo(denylists=active_denylists())
        for finding in findings:
            print(finding)
        return 1 if findings else 0
    print("usage: python -m tests.hygiene [scan | add <string>...]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
