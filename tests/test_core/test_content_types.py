"""An attachment's content type comes from Lattice's own table (LAT-356)."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest

from lattice.core.content_types import TYPES, guess_content_type


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.md", "text/markdown"),
        ("REPORT.MD", "text/markdown"),
        ("notes.markdown", "text/markdown"),
        ("guide.rst", "text/x-rst"),
        ("trace.jsonl", None),
        ("data.json", "application/json"),
        ("run.log", None),
        ("out.txt", "text/plain"),
        ("shot.png", "image/png"),
        ("logs.tar.gz", "application/x-tar"),
        ("logs.tgz", "application/x-tar"),
        ("icon.svgz", "image/svg+xml"),
        ("report.md.gz", "text/markdown"),
        ("report.md.GZ", None),
        ("Makefile", None),
        (".md", None),
        ("", None),
        # Read literally, not as a URL as mimetypes.guess_type did (G-6).
        ("data:report.md", "text/markdown"),
        ("release:report.md?1", None),
    ],
)
def test_guess(name: str, expected: str | None) -> None:
    assert guess_content_type(name) == expected


def test_the_table_is_lowercase() -> None:
    assert all(ext == ext.lower() and ext.startswith(".") for ext in TYPES)


def test_the_live_mimetypes_tables_never_enter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cause of LAT-356: CPython 3.12.0's built-in table has no ``.md``, and a
    host file (``/etc/apache2/mime.types``, ``/etc/mime.types``) can add or change
    entries. Neither reaches the guess."""
    host = tmp_path / "mime.types"
    host.write_text("text/x-host-markdown md\napplication/x-host-sqlite sqlite\n")
    builtin = {k: v for k, v in mimetypes._types_map_default.items() if k != ".md"}  # type: ignore[attr-defined]
    # mimetypes.init() rebinds these module globals; monkeypatch restores them.
    for name in ("types_map", "suffix_map", "encodings_map", "common_types"):
        monkeypatch.setattr(mimetypes, name, getattr(mimetypes, name))
    monkeypatch.setattr(mimetypes, "_types_map_default", builtin)
    monkeypatch.setattr(mimetypes, "knownfiles", [str(host)])
    monkeypatch.setattr(mimetypes, "_db", None)
    monkeypatch.setattr(mimetypes, "inited", False)
    mimetypes.init()
    assert mimetypes.guess_type("report.md")[0] == "text/x-host-markdown"
    assert guess_content_type("report.md") == "text/markdown"
    assert guess_content_type("board.sqlite") is None
