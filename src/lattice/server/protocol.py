"""Wire protocol constants and version comparison (SPEC §8.4, §15).

Standard library only: the client (H-11) reads the same constants.
"""

from __future__ import annotations

import re

#: The wire protocol. Only a change to the wire format itself bumps it.
PROTOCOL = 1

#: The oldest client this server's code accepts ops from (SPEC §15). A release
#: raises it when it adds an event type that changes snapshot materialization,
#: a newly synced durable path, or the default or meaning of an existing
#: operation parameter.
MIN_CLIENT_VERSION = "0.2.2"

HEADER_PROTOCOL = "Lattice-Protocol"
HEADER_SERVER_VERSION = "Lattice-Server-Version"
HEADER_MIN_CLIENT_VERSION = "Lattice-Min-Client-Version"
HEADER_CLIENT_VERSION = "Lattice-Client-Version"

_VERSION_RE = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>a|alpha|b|beta|rc|c)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?post[-_.]?(?P<post>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?"
    r"(?:\+[a-z0-9.]+)?$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2}


def server_version() -> str:
    """This process's Lattice version."""
    from lattice import __version__

    return __version__


def version_key(version: str) -> tuple | None:
    """A sort key for a PEP 440 version string, or ``None`` if it does not parse.

    Enough of PEP 440 for comparing Lattice releases: release segments (with
    trailing zeros ignored), then dev < pre < final < post.
    """
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre = match["pre"]
    dev = match["dev"]
    post = match["post"]
    if pre is not None:
        phase = (1, _PRE_RANK[pre.lower()], int(match["pre_n"] or 0))
    elif post is None and dev is not None:
        phase = (0, 0, 0)  # a bare dev release sorts before its pre-releases
    else:
        phase = (2, 0, 0)
    post_key = int(post or 0) if post is not None else -1
    dev_key = int(dev or 0) if dev is not None else float("inf")
    return (tuple(release), phase, post_key, dev_key)


def is_older(version: str, than: str) -> bool:
    """Whether *version* sorts before *than*. An unparseable version is never older."""
    a, b = version_key(version), version_key(than)
    if a is None or b is None:
        return False
    return a < b
