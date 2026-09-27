"""The page's third-party scripts are vendored (SPEC §10's CSP allows only
same-origin scripts; a local dashboard works offline): the page loads nothing
from another origin, and every vendored file is pinned, hashed, licensed, and
served."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.request import urlopen

from lattice.dashboard.server import STATIC_DIR

VENDOR = STATIC_DIR / "vendor"
REPO = Path(__file__).resolve().parents[2]


def _page() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_the_page_loads_nothing_from_another_origin() -> None:
    sources = re.findall(r"<(?:script|link)\b[^>]*\b(?:src|href)=\"([^\"]+)\"", _page())
    assert sources, "the page loads its scripts and styles"
    foreign = [s for s in sources if re.match(r"^(?:[a-z]+:)?//", s, re.I)]
    assert foreign == []
    assert "unpkg.com" not in _page()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_vendored_script_is_pinned_hashed_and_licensed() -> None:
    packages = json.loads((VENDOR / "VERSIONS.json").read_text())["packages"]
    page = _page()
    for package in packages:
        assert re.fullmatch(r"\d+\.\d+\.\d+", package["version"]), package
        assert package["spec"] == f"{package['name']}@{package['version']}", package
        assert package["integrity"].startswith("sha512-"), package
        path = VENDOR / package["file"]
        assert _sha256(path) == package["sha256"], package
        licenses = package["licenses"]
        assert licenses, package
        for name, digest in licenses.items():
            assert _sha256(path.parent / name) == digest, (package["name"], name)
        shipped = {p.name for p in path.parent.iterdir()}
        assert shipped == {path.name, *licenses}, package  # nothing unrecorded
        assert f'<script src="static/vendor/{package["file"]}"></script>' in page, package
    vendored = {p["file"] for p in packages}
    referenced = set(re.findall(r'src="static/vendor/([^"]+)"', page))
    assert referenced == vendored


def test_the_vendoring_script_pins_exactly_what_is_recorded() -> None:
    """Regenerating fetches the recorded versions, never a newer one in a range."""
    script = (REPO / "scripts" / "vendor-dashboard-js.sh").read_text()
    block = re.search(r"^PACKAGES=\((.*?)^\)", script, re.M | re.S)
    assert block, "the script lists its packages in PACKAGES=( ... )"
    specs = re.findall(r'"([^"|]+)\|[^"]*"', block.group(1))
    recorded = [p["spec"] for p in json.loads((VENDOR / "VERSIONS.json").read_text())["packages"]]
    assert specs == recorded
    for spec in specs:
        assert re.fullmatch(r"[a-z0-9-]+@\d+\.\d+\.\d+", spec), spec


def test_the_local_dashboard_serves_the_vendored_scripts(dashboard_server) -> None:
    base_url, _, _ = dashboard_server
    for file in re.findall(r'src="(static/vendor/[^"]+)"', _page()):
        with urlopen(f"{base_url}/{file}") as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("application/javascript")
            assert response.read() == (STATIC_DIR / file.removeprefix("static/")).read_bytes()
