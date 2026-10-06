"""Pinning tests for the c11 backend's ref parser.

c11 has spelled its refs three ways. The backend must parse all of them:

* <=0.66: ``OK surface:74 pane:46 workspace:7``
* 0.67:   ``OK tab:74 area:46 workspace:7``
* 1.0:    ``OK panel:74 area:46 workspace:7``

The tests exercise the parser directly without touching the c11 socket.
"""

from __future__ import annotations

import pytest

from lattice.integrations import c11
from lattice.integrations.c11 import _area_ref, _panel_ref, _parse_refs


class TestParseRefs:
    def test_workspace_only(self) -> None:
        assert _parse_refs("OK workspace:7") == {"workspace": "workspace:7"}

    def test_generation_066_surface_pane(self) -> None:
        refs = _parse_refs("OK surface:74 pane:46 workspace:7")
        assert refs == {
            "surface": "surface:74",
            "pane": "pane:46",
            "workspace": "workspace:7",
        }

    def test_generation_067_tab_area(self) -> None:
        refs = _parse_refs("OK tab:74 area:46 workspace:7")
        assert refs == {
            "tab": "tab:74",
            "area": "area:46",
            "workspace": "workspace:7",
        }

    def test_generation_100_panel_area(self) -> None:
        refs = _parse_refs("OK panel:74 area:46 workspace:7")
        assert refs == {
            "panel": "panel:74",
            "area": "area:46",
            "workspace": "workspace:7",
        }

    def test_listing_line_with_selected_marker(self) -> None:
        # `c11 list-pane-surfaces` returns "* <ref>  …  [selected]"
        assert _parse_refs("* surface:75  …/code/c11  [selected]") == {"surface": "surface:75"}
        assert _parse_refs("* tab:75  …/code/c11  [selected]") == {"tab": "tab:75"}
        assert _parse_refs("* panel:75  …/code/c11  [selected]") == {"panel": "panel:75"}

    def test_picks_first_per_kind(self) -> None:
        refs = _parse_refs("OK panel:1 area:2 workspace:3 panel:4 area:5")
        assert refs["panel"] == "panel:1"
        assert refs["area"] == "area:2"
        assert refs["workspace"] == "workspace:3"

    def test_ignores_embedded_word_fragments(self) -> None:
        assert _parse_refs("OK default_tab:9 subpanel:3") == {}

    def test_empty(self) -> None:
        assert _parse_refs("") == {}

    def test_no_refs(self) -> None:
        assert _parse_refs("Error: not_found") == {}


class TestPanelAndAreaRefs:
    @pytest.mark.parametrize(
        ("text", "panel", "area"),
        [
            ("OK surface:74 pane:46 workspace:7", "surface:74", "pane:46"),
            ("OK tab:74 area:46 workspace:7", "tab:74", "area:46"),
            ("OK panel:74 area:46 workspace:7", "panel:74", "area:46"),
        ],
    )
    def test_each_generation(self, text: str, panel: str, area: str) -> None:
        refs = _parse_refs(text)
        assert _panel_ref(refs) == panel
        assert _area_ref(refs) == area

    def test_missing(self) -> None:
        refs = _parse_refs("OK workspace:7")
        assert _panel_ref(refs) is None
        assert _area_ref(refs) is None

    def test_prefers_panel_spelling_when_several_present(self) -> None:
        # A build that emits both generations' keys (1.0 prints panel + legacy tab).
        refs = _parse_refs("OK panel:74 tab:74 area:46")
        assert _panel_ref(refs) == "panel:74"


_GENERATIONS = [
    pytest.param("OK surface:74 pane:46 workspace:7", "surface:74", "pane:46", id="pre-0.67"),
    pytest.param("OK tab:74 area:46 workspace:7", "tab:74", "area:46", id="0.67"),
    pytest.param("OK panel:74 area:46 workspace:7", "panel:74", "area:46", id="1.0"),
]


class TestCallersPickRightRef:
    """The callers, not just the parser, must hand back the ref c11 spoke."""

    @pytest.mark.parametrize(("text", "panel", "area"), _GENERATIONS)
    def test_new_pane(
        self, monkeypatch: pytest.MonkeyPatch, text: str, panel: str, area: str
    ) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_capture(*args: str) -> str | None:
            calls.append(args)
            return text

        monkeypatch.setattr(c11, "_c11_capture", fake_capture)
        slot = c11._new_pane("workspace:7", direction="right")
        assert slot is not None
        assert slot.surface_ref == panel
        assert slot.pane_ref == area
        assert calls[0][0] == "new-pane"

    @pytest.mark.parametrize(("text", "panel", "area"), _GENERATIONS)
    def test_initial_surface_from_listing(
        self, monkeypatch: pytest.MonkeyPatch, text: str, panel: str, area: str
    ) -> None:
        listing = f"* {panel}  …/code/c11  [selected]"
        monkeypatch.setattr(c11, "_c11_capture", lambda *args: listing)
        assert c11._initial_surface("workspace:7") == panel

    @pytest.mark.parametrize(("text", "panel", "area"), _GENERATIONS)
    def test_initial_surface_falls_back_to_tree(
        self, monkeypatch: pytest.MonkeyPatch, text: str, panel: str, area: str
    ) -> None:
        tree = f"workspace:7\n  {area}\n    {panel}  Terminal\n"

        def fake_capture(*args: str) -> str | None:
            return None if args[0] == "list-pane-surfaces" else tree

        monkeypatch.setattr(c11, "_c11_capture", fake_capture)
        assert c11._initial_surface("workspace:7") == panel

    def test_new_pane_without_panel_ref_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(c11, "_c11_capture", lambda *args: "OK workspace:7")
        assert c11._new_pane("workspace:7", direction="down") is None
