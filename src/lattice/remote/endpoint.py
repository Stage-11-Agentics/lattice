"""Where a hosted checkout's server is: the remote a follower connects to.

Resolving a checkout to its remote (``remotes.json``, environment overrides,
the binding) is H-11's. Until it lands no checkout is hosted:
:func:`hosted_root` returns ``None`` and every command stays local.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class RemoteEndpoint:
    """One project on one server, with the credentials to reach it.

    ``token`` and ``headers`` never appear in ``repr``.
    """

    alias: str
    url: str
    project: str
    token: str = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def api(self, path: str) -> str:
        """The absolute URL of *path* under this project (``/v1/projects/<slug>/<path>``)."""
        base = self.url.rstrip("/")
        return f"{base}/v1/projects/{quote(self.project, safe='')}/{path.lstrip('/')}"

    def root(self, path: str) -> str:
        """The absolute URL of a server-level *path* (for example ``/v1/info``)."""
        return f"{self.url.rstrip('/')}/{path.lstrip('/')}"


def hosted_root(start: Path) -> Path | None:
    """The hosted root a command started in *start* belongs to, or ``None`` (H-11)."""
    return None


def endpoint_for(root: Path) -> RemoteEndpoint:
    """The remote *root*'s cache is bound to (H-11)."""
    raise NotImplementedError("remote resolution is provided by binding and routing (H-11)")
