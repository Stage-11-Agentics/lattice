"""The page's third-party scripts are vendored (SPEC §10's CSP allows only
same-origin scripts; a local dashboard works offline): the page loads nothing
from another origin, and every vendored file is pinned, hashed, licensed, and
served."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.request import urlopen

from lattice.dashboard.server import STATIC_DIR

VENDOR = STATIC_DIR / "vendor"


def _page() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_the_page_loads_nothing_from_another_origin() -> None:
    sources = re.findall(r"<(?:script|link)\b[^>]*\b(?:src|href)=\"([^\"]+)\"", _page())
    assert sources, "the page loads its scripts and styles"
    foreign = [s for s in sources if re.match(r"^(?:[a-z]+:)?//", s, re.I)]
    assert foreign == []
    assert "unpkg.com" not in _page()


def test_every_vendored_script_is_pinned_hashed_and_licensed() -> None:
    packages = json.loads((VENDOR / "VERSIONS.json").read_text())["packages"]
    page = _page()
    for package in packages:
        assert re.fullmatch(r"\d+\.\d+\.\d+", package["version"]), package
        assert package["integrity"].startswith("sha512-"), package
        path = VENDOR / package["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == package["sha256"], package
        assert package["licenses"] and all(
            (path.parent / name).is_file() for name in package["licenses"]
        ), package
        assert f'<script src="static/vendor/{package["file"]}"></script>' in page, package
    vendored = {p["file"] for p in packages}
    referenced = set(re.findall(r'src="static/vendor/([^"]+)"', page))
    assert referenced == vendored


def test_the_local_dashboard_serves_the_vendored_scripts(dashboard_server) -> None:
    base_url, _, _ = dashboard_server
    for file in re.findall(r'src="(static/vendor/[^"]+)"', _page()):
        with urlopen(f"{base_url}/{file}") as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("application/javascript")
            assert response.read() == (STATIC_DIR / file.removeprefix("static/")).read_bytes()
