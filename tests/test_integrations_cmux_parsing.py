"""Pinning tests for the cmux backend's ref parser.

The cmux CLI returns refs as "OK workspace:N" or "OK surface:N pane:M
workspace:K". The backend depends on stable parsing of those lines —
exercises the parser directly without touching the cmux socket.
"""

from __future__ import annotations

from lattice.integrations.cmux import _parse_refs


class TestParseRefs:
    def test_workspace_only(self) -> None:
        assert _parse_refs("OK workspace:7") == {"workspace": "workspace:7"}

    def test_workspace_pane_surface(self) -> None:
        refs = _parse_refs("OK surface:74 pane:46 workspace:7")
        assert refs == {
            "surface": "surface:74",
            "pane": "pane:46",
            "workspace": "workspace:7",
        }

    def test_surface_first(self) -> None:
        # `cmux list-pane-surfaces` returns "* surface:N  …  [selected]"
        refs = _parse_refs("* surface:75  …/Stage11/code/cmux  [selected]")
        assert refs == {"surface": "surface:75"}

    def test_picks_first_per_kind(self) -> None:
        text = "OK surface:1 pane:2 workspace:3 surface:4 pane:5"
        refs = _parse_refs(text)
        assert refs["surface"] == "surface:1"
        assert refs["pane"] == "pane:2"
        assert refs["workspace"] == "workspace:3"

    def test_empty(self) -> None:
        assert _parse_refs("") == {}

    def test_no_refs(self) -> None:
        assert _parse_refs("Error: not_found") == {}
